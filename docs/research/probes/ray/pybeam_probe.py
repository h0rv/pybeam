import asyncio
import json
import os
import platform
import statistics
import sys
import time
from importlib.metadata import version

import psutil

import pybeam

ROOT_PID = int(os.environ.setdefault("PROBE_ROOT_PID", str(os.getpid())))
ITERATIONS = int(os.environ.get("PROBE_ITERATIONS", "12000000"))


@pybeam.actor
class Counter:
    value: int = 0

    async def add(self, amount: int) -> int:
        self.value += amount
        return self.value

    async def cpu(self, iterations: int) -> int:
        total = 0
        for index in range(iterations):
            total += (index * index) % 97
        return total


def tree_rss() -> int:
    root = psutil.Process(ROOT_PID)
    total = 0
    for process in [root, *root.children(recursive=True)]:
        try:
            total += process.memory_info().rss
        except psutil.NoSuchProcess:
            continue
    return total


async def caller(counter: Counter) -> None:
    for _ in range(100):
        await counter.add(1)


async def main() -> None:
    count = 1000
    cpus = len(os.sched_getaffinity(0))
    async with pybeam.ActorGroup(max_children=count + 1, max_pending=128) as group:
        first = await group.spawn(Counter)
        if await first.add(0) != 0:
            raise AssertionError("Cold actor state mismatch")
        cold = time.time() - psutil.Process(ROOT_PID).create_time()
        started = time.perf_counter()
        actors: list[Counter] = []
        for _ in range(count):
            actor = await group.spawn(Counter)
            if await actor.add(1) != 1:
                raise AssertionError("Spawned actor state mismatch")
            actors.append(actor)
        spawn = time.perf_counter() - started
        rss = tree_rss()
        for actor in actors:
            await group.stop(actor)
        samples: list[float] = []
        started = time.perf_counter()
        for _ in range(10000):
            call_start = time.perf_counter_ns()
            await first.add(1)
            samples.append((time.perf_counter_ns() - call_start) / 1000)
        sequential = time.perf_counter() - started
        if await first.add(0) != 10000:
            raise AssertionError("Sequential count mismatch")
        await first.add(-10000)
        started = time.perf_counter()
        async with asyncio.TaskGroup() as callers:
            for _ in range(100):
                callers.create_task(caller(first))
        concurrent = time.perf_counter() - started
        final = await first.add(0)
        if final != 10000:
            raise AssertionError("Concurrent count mismatch")
        workers = [await group.spawn(Counter) for _ in range(cpus)]
        for actor in workers:
            await actor.cpu(1)
        started = time.perf_counter()
        expected = await workers[0].cpu(ITERATIONS)
        single = time.perf_counter() - started
        started = time.perf_counter()
        async with asyncio.TaskGroup() as tasks:
            pending = [tasks.create_task(actor.cpu(ITERATIONS)) for actor in workers]
        parallel = time.perf_counter() - started
        if not all(task.result() == expected for task in pending):
            raise AssertionError("CPU result mismatch")
    print(
        json.dumps(
            {
                "framework": "pybeam",
                "trial": int(os.environ.get("PROBE_TRIAL", "1")),
                "python": platform.python_version(),
                "version": version("python-beam"),
                "nproc": cpus,
                "ram_gib": psutil.virtual_memory().total / 2**30,
                "gil_enabled": sys._is_gil_enabled(),
                "cold_s": cold,
                "spawn_requested": count,
                "spawn_n": len(actors),
                "spawn_stop": "target",
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
        ),
        flush=True,
    )


if __name__ == "__main__":
    pybeam.run(main)
