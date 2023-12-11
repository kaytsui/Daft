from __future__ import annotations

import logging
import multiprocessing
from concurrent import futures
from dataclasses import dataclass
from typing import Iterable, Iterator

import psutil

from daft.daft import (
    FileFormatConfig,
    FileInfos,
    IOConfig,
    PyDaftConfig,
    ResourceRequest,
    StorageConfig,
)
from daft.execution import physical_plan
from daft.execution.execution_step import Instruction, PartitionTask
from daft.filesystem import glob_path_with_stats
from daft.internal.gpu import cuda_device_count
from daft.logical.builder import LogicalPlanBuilder
from daft.logical.schema import Schema
from daft.runners import runner_io
from daft.runners.partitioning import (
    MaterializedResult,
    PartID,
    PartitionCacheEntry,
    PartitionMetadata,
    PartitionSet,
)
from daft.runners.profiler import profiler
from daft.runners.progress_bar import ProgressBar
from daft.runners.runner import Runner
from daft.table import MicroPartition

logger = logging.getLogger(__name__)


@dataclass
class LocalPartitionSet(PartitionSet[MicroPartition]):
    _partitions: dict[PartID, MicroPartition]

    def items(self) -> list[tuple[PartID, MicroPartition]]:
        return sorted(self._partitions.items())

    def _get_merged_vpartition(self) -> MicroPartition:
        ids_and_partitions = self.items()
        assert ids_and_partitions[0][0] == 0
        assert ids_and_partitions[-1][0] + 1 == len(ids_and_partitions)
        return MicroPartition.concat([part for id, part in ids_and_partitions])

    def get_partition(self, idx: PartID) -> MicroPartition:
        return self._partitions[idx]

    def set_partition(self, idx: PartID, part: MaterializedResult[MicroPartition]) -> None:
        self._partitions[idx] = part.partition()

    def delete_partition(self, idx: PartID) -> None:
        del self._partitions[idx]

    def has_partition(self, idx: PartID) -> bool:
        return idx in self._partitions

    def __len__(self) -> int:
        return sum(len(partition) for partition in self._partitions.values())

    def size_bytes(self) -> int | None:
        size_bytes_ = [partition.size_bytes() for partition in self._partitions.values()]
        size_bytes: list[int] = [size for size in size_bytes_ if size is not None]
        if len(size_bytes) != len(size_bytes_):
            return None
        else:
            return sum(size_bytes)

    def num_partitions(self) -> int:
        return len(self._partitions)

    def wait(self) -> None:
        pass


class PyRunnerIO(runner_io.RunnerIO):
    def glob_paths_details(
        self,
        source_paths: list[str],
        file_format_config: FileFormatConfig | None = None,
        io_config: IOConfig | None = None,
    ) -> FileInfos:
        file_infos = FileInfos()
        file_format = file_format_config.file_format() if file_format_config is not None else None
        for source_path in source_paths:
            path_file_infos = glob_path_with_stats(source_path, file_format, io_config)

            if len(path_file_infos) == 0:
                raise FileNotFoundError(f"No files found at {source_path}")

            file_infos.extend(path_file_infos)

        return file_infos

    def get_schema_from_first_filepath(
        self,
        file_infos: FileInfos,
        file_format_config: FileFormatConfig,
        storage_config: StorageConfig,
    ) -> Schema:
        if len(file_infos) == 0:
            raise ValueError("No files to get schema from")
        # Naively retrieve the first filepath in the PartitionSet
        return runner_io.sample_schema(file_infos[0].file_path, file_format_config, storage_config)


class PyRunner(Runner[MicroPartition]):
    def __init__(self, daft_config: PyDaftConfig, use_thread_pool: bool | None) -> None:
        super().__init__()
        self.daft_config = daft_config
        self._use_thread_pool: bool = use_thread_pool if use_thread_pool is not None else True

        self.num_cpus = multiprocessing.cpu_count()
        self.num_gpus = cuda_device_count()
        self.bytes_memory = psutil.virtual_memory().total

    def runner_io(self) -> PyRunnerIO:
        return PyRunnerIO()

    def run(self, builder: LogicalPlanBuilder) -> PartitionCacheEntry:
        results = list(self.run_iter(builder))

        result_pset = LocalPartitionSet({})
        for i, result in enumerate(results):
            result_pset.set_partition(i, result)

        pset_entry = self.put_partition_set_into_cache(result_pset)
        return pset_entry

    def run_iter(
        self,
        builder: LogicalPlanBuilder,
        # NOTE: PyRunner does not run any async execution, so it ignores `results_buffer_size` which is essentially 0
        results_buffer_size: int | None = None,
    ) -> Iterator[PyMaterializedResult]:
        # Optimize the logical plan.
        builder = builder.optimize()
        # Finalize the logical plan and get a physical plan scheduler for translating the
        # physical plan to executable tasks.
        plan_scheduler = builder.to_physical_plan_scheduler(self.daft_config)
        psets = {
            key: entry.value.values()
            for key, entry in self._part_set_cache._uuid_to_partition_set.items()
            if entry.value is not None
        }
        # Get executable tasks from planner.
        tasks = plan_scheduler.to_partition_tasks(psets, is_ray_runner=False)
        with profiler("profile_PyRunner.run_{datetime.now().isoformat()}.json"):
            results_gen = self._physical_plan_to_partitions(tasks)
            yield from results_gen

    def run_iter_tables(
        self, builder: LogicalPlanBuilder, results_buffer_size: int | None = None
    ) -> Iterator[MicroPartition]:
        for result in self.run_iter(builder, results_buffer_size=results_buffer_size):
            yield result.partition()

    def _physical_plan_to_partitions(
        self, plan: physical_plan.MaterializedPhysicalPlan[MicroPartition]
    ) -> Iterator[PyMaterializedResult]:
        inflight_tasks: dict[str, PartitionTask] = dict()
        inflight_tasks_resources: dict[str, ResourceRequest] = dict()
        future_to_task: dict[futures.Future, str] = dict()

        pbar = ProgressBar(use_ray_tqdm=False)
        with futures.ThreadPoolExecutor() as thread_pool:
            try:
                next_step = next(plan)

                # Dispatch->Await loop.
                while True:
                    # Dispatch loop.
                    while True:
                        if next_step is None:
                            # Blocked on already dispatched tasks; await some tasks.
                            break

                        elif isinstance(next_step, MaterializedResult):
                            assert isinstance(next_step, PyMaterializedResult)

                            # A final result.
                            yield next_step
                            next_step = next(plan)
                            continue

                        elif not self._can_admit_task(next_step.resource_request, inflight_tasks_resources.values()):
                            # Insufficient resources; await some tasks.
                            break

                        else:
                            # next_task is a task to run.

                            # Run the task in the main thread, instead of the thread pool, in certain conditions:
                            # - Threading is disabled in runner config.
                            # - Task is a no-op.
                            # - Task requires GPU.
                            # TODO(charles): Queue these up until the physical plan is blocked to avoid starving cluster.
                            if (
                                not self._use_thread_pool
                                or len(next_step.instructions) == 0
                                or (
                                    next_step.resource_request.num_gpus is not None
                                    and next_step.resource_request.num_gpus > 0
                                )
                            ):
                                logger.debug("Running task synchronously in main thread: %s", next_step)
                                partitions = self.build_partitions(next_step.instructions, *next_step.inputs)
                                next_step.set_result([PyMaterializedResult(partition) for partition in partitions])

                            else:
                                # Submit the task for execution.
                                logger.debug("Submitting task for execution: %s", next_step)

                                # update progress bar
                                pbar.mark_task_start(next_step)

                                future = thread_pool.submit(
                                    self.build_partitions, next_step.instructions, *next_step.inputs
                                )
                                # Register the inflight task and resources used.
                                future_to_task[future] = next_step.id()

                                inflight_tasks[next_step.id()] = next_step
                                inflight_tasks_resources[next_step.id()] = next_step.resource_request

                            next_step = next(plan)

                    # Await at least one task and process the results.
                    assert (
                        len(future_to_task) > 0
                    ), f"Scheduler deadlocked! This should never happen. Please file an issue."
                    done_set, _ = futures.wait(list(future_to_task.keys()), return_when=futures.FIRST_COMPLETED)
                    for done_future in done_set:
                        done_id = future_to_task.pop(done_future)
                        del inflight_tasks_resources[done_id]
                        done_task = inflight_tasks.pop(done_id)
                        partitions = done_future.result()

                        pbar.mark_task_done(done_task)

                        logger.debug("Task completed: %s -> <%s partitions>", done_id, len(partitions))
                        done_task.set_result([PyMaterializedResult(partition) for partition in partitions])

                    if next_step is None:
                        next_step = next(plan)

            except StopIteration:
                pbar.close()
                return

    def _check_resource_requests(self, resource_request: ResourceRequest) -> None:
        """Validates that the requested ResourceRequest is possible to run locally"""

        if resource_request.num_cpus is not None and resource_request.num_cpus > self.num_cpus:
            raise RuntimeError(f"Requested {resource_request.num_cpus} CPUs but found only {self.num_cpus} available")
        if resource_request.num_gpus is not None and resource_request.num_gpus > self.num_gpus:
            raise RuntimeError(f"Requested {resource_request.num_gpus} GPUs but found only {self.num_gpus} available")
        if resource_request.memory_bytes is not None and resource_request.memory_bytes > self.bytes_memory:
            raise RuntimeError(
                f"Requested {resource_request.memory_bytes} bytes of memory but found only {self.bytes_memory} available"
            )

    def _can_admit_task(self, resource_request: ResourceRequest, inflight_resources: Iterable[ResourceRequest]) -> bool:
        self._check_resource_requests(resource_request)

        total_inflight_resources: ResourceRequest = sum(inflight_resources, ResourceRequest())
        cpus_okay = (total_inflight_resources.num_cpus or 0) + (resource_request.num_cpus or 0) <= self.num_cpus
        gpus_okay = (total_inflight_resources.num_gpus or 0) + (resource_request.num_gpus or 0) <= self.num_gpus
        memory_okay = (total_inflight_resources.memory_bytes or 0) + (
            resource_request.memory_bytes or 0
        ) <= self.bytes_memory

        return all((cpus_okay, gpus_okay, memory_okay))

    @staticmethod
    def build_partitions(instruction_stack: list[Instruction], *inputs: MicroPartition) -> list[MicroPartition]:
        partitions = list(inputs)
        for instruction in instruction_stack:
            partitions = instruction.run(partitions)

        return partitions


@dataclass(frozen=True)
class PyMaterializedResult(MaterializedResult[MicroPartition]):
    _partition: MicroPartition

    def partition(self) -> MicroPartition:
        return self._partition

    def vpartition(self) -> MicroPartition:
        return self._partition

    def metadata(self) -> PartitionMetadata:
        return PartitionMetadata.from_table(self._partition)

    def cancel(self) -> None:
        return None

    def _noop(self, _: MicroPartition) -> None:
        return None
