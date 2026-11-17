# pybeam_actors.py
import asyncio
import http.client
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pybeam
from pybeam.actors.groups import context_counts
from pybeam.codec import Json
from pybeam.tasks import TaskFunction, TaskLimits, TaskSupervisor

CHECKPOINTS = (1, 10, 100, 1000, 2000, 5000, 10000)
DEADLINE_S = float(os.environ.get("PROBE_DEADLINE_S", "180"))
PORT = int(os.environ.get("PROBE_PORT", "0"))


def status() -> tuple[int, int]:
    fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    return int(fields["VmRSS"].split()[0]), int(fields["Threads"])


def fetch() -> int:
    connection = http.client.HTTPConnection("127.0.0.1", PORT)
    try:
        connection.request("GET", "/")
        return len(connection.getresponse().read())
    finally:
        connection.close()


@pybeam.actor
class Worker:
    calls: int = 0

    async def noop(self) -> int:
        self.calls += 1
        return self.calls

    async def where(self) -> int:
        return threading.get_ident()

    async def wait(self, seconds: float) -> float:
        await asyncio.sleep(seconds)
        return seconds

    async def block(self, seconds: float) -> float:
        time.sleep(seconds)  # noqa: ASYNC251
        return seconds

    async def offload(self, seconds: float) -> float:
        await asyncio.to_thread(time.sleep, seconds)
        return seconds

    async def fetch_serial(self, count: int) -> int:
        return sum(fetch() for _ in range(count))

    async def fetch_threads(self, count: int) -> int:
        with ThreadPoolExecutor(count) as pool:
            return sum(pool.map(lambda _: fetch(), range(count)))

    async def fetch_to_thread(self, count: int) -> int:
        sizes = await asyncio.gather(*(asyncio.to_thread(fetch) for _ in range(count)))
        return sum(sizes)


async def blocking_task(seconds: float) -> float:
    time.sleep(seconds)  # noqa: ASYNC251
    return seconds


async def main() -> None:
    results: dict[str, object] = {}
    rss0, threads0 = status()
    results["baseline"] = {"rss_kib": rss0, "threads": threads0}
    spawn_rows: list[dict[str, object]] = []
    began = time.perf_counter()
    async with pybeam.ActorGroup(max_children=max(CHECKPOINTS), max_pending=64) as group:
        actors: list[Worker] = []
        stop_reason = "reached last checkpoint"
        for checkpoint in CHECKPOINTS:
            start = time.perf_counter()
            try:
                while len(actors) < checkpoint:
                    if time.perf_counter() - began > DEADLINE_S:
                        raise TimeoutError("deadline")
                    actors.append(await group.spawn(Worker))
            except Exception as error:
                stop_reason = type(error).__name__ + ": " + str(error)[:200]
                break
            rss, threads = status()
            counts = await context_counts()
            spawn_rows.append(
                {
                    "actors": len(actors),
                    "rss_kib": rss,
                    "threads": threads,
                    "live_contexts": counts.live,
                    "segment_spawn_s": round(time.perf_counter() - start, 3),
                }
            )
            print(spawn_rows[-1], flush=True)
        results["spawn"] = spawn_rows
        results["spawn_stop"] = stop_reason
        results["distinct_threads_first_8"] = len({await a.where() for a in actors[:8]})
        results["main_thread_is_actor_thread"] = await actors[0].where() == threading.get_ident()

        first = actors[0]
        start = time.perf_counter()
        for _ in range(1000):
            await first.noop()
        results["noop_call_us"] = round((time.perf_counter() - start) * 1000, 1)

        io: dict[str, float | str] = {}
        for n in (1, 10, 100, 1000, 4000, 4100, 8000):
            if n > len(actors):
                continue
            start = time.perf_counter()
            try:
                await asyncio.gather(*(a.wait(0.05) for a in actors[:n]))
            except RuntimeError as error:
                io["wait_50ms_x" + str(n)] = str(error)
                await asyncio.sleep(1)
                continue
            io["wait_50ms_x" + str(n)] = round(time.perf_counter() - start, 3)
        for n in (1, 8, 32):
            start = time.perf_counter()
            await asyncio.gather(*(a.block(0.05) for a in actors[:n]))
            io["block_50ms_x" + str(n)] = round(time.perf_counter() - start, 3)
        for n in (8, 32, 100):
            start = time.perf_counter()
            await asyncio.gather(*(a.offload(0.05) for a in actors[:n]))
            io["to_thread_50ms_x" + str(n)] = round(time.perf_counter() - start, 3)
        start = time.perf_counter()
        await asyncio.gather(actors[0].block(0.3), actors[1].wait(0.01))
        io["wait_10ms_beside_block_300ms"] = round(time.perf_counter() - start, 3)
        results["actor_calls_s"] = io

        if PORT:
            fan: dict[str, float] = {}
            for name in ("fetch_serial", "fetch_threads", "fetch_to_thread"):
                start = time.perf_counter()
                await getattr(first, name)(10)
                fan[name + "_10"] = round(time.perf_counter() - start, 3)
            start = time.perf_counter()
            await asyncio.gather(*(a.fetch_to_thread(10) for a in actors[:10]))
            fan["fetch_to_thread_10_x10_actors"] = round(time.perf_counter() - start, 3)
            results["fanout_s"] = fan
        rss, threads = status()
        results["after_calls"] = {"rss_kib": rss, "threads": threads}

    tasks: dict[str, float] = {}
    for dedicated in (False, True):
        job = TaskFunction(blocking_task, Json(float), Json(float), dedicated_loop=dedicated)
        for n in (8, 64):
            limits = TaskLimits(capacity=n, max_bytes=1 << 20, shutdown_ms=5000)
            async with TaskSupervisor(limits) as supervisor:
                start = time.perf_counter()
                refs = [await supervisor.start(job, 0.05) for _ in range(n)]
                for ref in refs:
                    await ref
                key = ("dedicated" if dedicated else "shared") + "_block_50ms_x" + str(n)
                tasks[key] = round(time.perf_counter() - start, 3)
    results["tasks_s"] = tasks
    print("RESULT " + json.dumps(results), flush=True)


if __name__ == "__main__":
    pybeam.run(main)
