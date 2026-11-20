import asyncio
import contextlib
import json
import os
import platform
import statistics
import sys
import time
from typing import Protocol

import psutil
import ray
from ray.actor import ActorClass, ActorHandle

ITERATIONS = int(os.environ.get("PROBE_ITERATIONS", "12000000"))


class RemoteInt(Protocol):
    def remote(self, value: int) -> ray.ObjectRef[int]: ...


class CounterHandle(Protocol):
    @property
    def add(self) -> RemoteInt: ...
    @property
    def cpu(self) -> RemoteInt: ...


class Counter:
    def __init__(self) -> None:
        self.value = 0

    def add(self, amount: int) -> int:
        self.value += amount
        return self.value

    def cpu(self, iterations: int) -> int:
        total = 0
        for index in range(iterations):
            total += (index * index) % 97
        return total


def spawn_actor(factory: ActorClass[Counter]) -> CounterHandle:
    handle = factory.remote()
    if isinstance(handle, type):
        raise TypeError("Expected a remote actor handle, not a local class")
    return handle


def tree_usage() -> tuple[int, int]:
    root = psutil.Process()
    rss, threads = 0, 0
    for process in [root, *root.children(recursive=True)]:
        try:
            rss += process.memory_info().rss
            threads += process.num_threads()
        except psutil.NoSuchProcess:
            continue
    return rss, threads


async def caller(counter: CounterHandle) -> None:
    for _ in range(100):
        await counter.add.remote(1)


async def compute(counter: CounterHandle) -> int:
    return await counter.cpu.remote(ITERATIONS)


async def main() -> dict[str, object]:
    count = 1000
    cpus = len(os.sched_getaffinity(0))
    ray.init(
        num_cpus=cpus, include_dashboard=False, log_to_driver=False, object_store_memory=256 * 2**20
    )
    factory = ray.remote(num_cpus=0)(Counter)
    first = spawn_actor(factory)
    if await first.add.remote(0) != 0:
        raise AssertionError("Cold actor state mismatch")
    cold = time.time() - psutil.Process().create_time()
    actors: list[CounterHandle] = []
    stop = "target"
    started = time.perf_counter()
    for _ in range(count):
        rss, threads = tree_usage()
        if rss > 8 * 2**30 or psutil.virtual_memory().available < 4 * 2**30:
            stop = "memory_guard_8gib_tree_or_4gib_available"
            break
        if threads > 8000:
            stop = "thread_guard_8000"
            break
        if time.perf_counter() - started > 90:
            stop = "time_guard_90s"
            break
        actor = spawn_actor(factory)
        if await actor.add.remote(1) != 1:
            raise AssertionError("Spawned actor state mismatch")
        actors.append(actor)
    spawn = time.perf_counter() - started
    rss, _ = tree_usage()
    if not actors:
        raise RuntimeError("Resource guard prevented all spawns")
    for actor in actors:
        if not isinstance(actor, ActorHandle):
            raise TypeError("Expected a native Ray actor handle")
        ray.kill(actor)
    samples: list[float] = []
    started = time.perf_counter()
    for _ in range(10000):
        call_start = time.perf_counter_ns()
        await first.add.remote(1)
        samples.append((time.perf_counter_ns() - call_start) / 1000)
    sequential = time.perf_counter() - started
    if await first.add.remote(0) != 10000:
        raise AssertionError("Sequential count mismatch")
    await first.add.remote(-10000)
    started = time.perf_counter()
    async with asyncio.TaskGroup() as callers:
        for _ in range(100):
            callers.create_task(caller(first))
    concurrent = time.perf_counter() - started
    final = await first.add.remote(0)
    if final != 10000:
        raise AssertionError("Concurrent count mismatch")
    workers = [spawn_actor(factory) for _ in range(cpus)]
    for actor in workers:
        await actor.cpu.remote(1)
    started = time.perf_counter()
    expected = await workers[0].cpu.remote(ITERATIONS)
    single = time.perf_counter() - started
    started = time.perf_counter()
    async with asyncio.TaskGroup() as tasks:
        pending = [tasks.create_task(compute(actor)) for actor in workers]
    parallel = time.perf_counter() - started
    if not all(task.result() == expected for task in pending):
        raise AssertionError("CPU result mismatch")
    return {
        "framework": "ray",
        "trial": int(os.environ.get("PROBE_TRIAL", "1")),
        "python": platform.python_version(),
        "version": ray.__version__,
        "nproc": cpus,
        "ram_gib": psutil.virtual_memory().total / 2**30,
        "gil_enabled": sys._is_gil_enabled(),
        "cold_s": cold,
        "spawn_requested": count,
        "spawn_n": len(actors),
        "spawn_stop": stop,
        "spawn_s": spawn,
        "spawn_us_each": spawn * 1e6 / len(actors),
        "tree_rss_mib": rss / 2**20,
        "latency_n": len(samples),
        "p50_us": statistics.median(samples),
        "p99_us": sorted(samples)[9899],
        "calls_s": 10000 / sequential,
        "concurrent_s": concurrent,
        "concurrent_calls_s": 10000 / concurrent,
        "concurrent_final": final,
        "cpu_k": cpus,
        "cpu_iterations": ITERATIONS,
        "cpu_single_s": single,
        "cpu_parallel_s": parallel,
        "cpu_speedup": cpus * single / parallel,
    }


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(main())
    finally:
        ray.shutdown()
    print(json.dumps(result), flush=True)
