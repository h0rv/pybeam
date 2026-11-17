import argparse
import asyncio
import json
import multiprocessing
import os
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Literal

from examples.modes_lab.job import crash, noop, spin

type Baseline = Literal["asyncio", "threads", "spawn", "prefork"]
type Span = dict[str, int | bool]


def rss_tree() -> int:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            parents[int(entry.name)] = int(stat.rsplit(") ", 1)[1].split()[1])
    tree = {os.getpid()}
    grown = True
    while grown:
        children = {pid for pid, parent in parents.items() if parent in tree} - tree
        tree |= children
        grown = bool(children)
    return sum(rss(pid) for pid in tree)


def rss(pid: int) -> int:
    try:
        status = Path("/proc", str(pid), "status").read_text()
    except OSError:
        return 0
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def micros(begin: int) -> int:
    return (time.monotonic_ns() - begin) // 1000


def latency(call: Callable[[], object]) -> list[int]:
    samples = []
    for _ in range(200):
        begin = time.monotonic_ns()
        call()
        samples.append(micros(begin))
    return sorted(samples)


def percentile(samples: list[int], rank: int) -> int:
    return samples[max(0, (len(samples) * rank + 99) // 100 - 1)]


def spawned(size: int, queue: multiprocessing.Queue[Span]) -> None:
    span = spin(size)
    span["rss_kib"] = rss(os.getpid())
    queue.put(span)


async def fanout(kind: Baseline, count: int, size: int) -> dict[str, object]:
    base = rss_tree()
    single = spin(size)
    create: list[int] = []
    samples: list[int] = []
    begin = time.monotonic_ns()
    match kind:
        case "asyncio":

            async def job() -> Span:
                return spin(size)

            samples = latency(noop)
            begin = time.monotonic_ns()
            spans = await asyncio.gather(*(job() for _ in range(count)))
            wall = micros(begin)
            extra = 0
        case "threads":
            with ThreadPoolExecutor(1) as executor:
                samples = latency(lambda: executor.submit(noop).result())
            begin = time.monotonic_ns()
            spans = await asyncio.gather(*(asyncio.to_thread(spin, size) for _ in range(count)))
            wall = micros(begin)
            extra = rss_tree() - base
        case "spawn":
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            workers = []
            for _ in range(count):
                started = time.monotonic_ns()
                process = context.Process(target=spawned, args=(size, queue))
                process.start()
                workers.append(process)
                create.append(micros(started))
            spans = [await asyncio.to_thread(queue.get) for _ in workers]
            wall = micros(begin)
            extra = sum(int(span.pop("rss_kib")) for span in spans)
            for process in workers:
                process.join()
        case "prefork":
            started = time.monotonic_ns()
            with ProcessPoolExecutor(
                count, mp_context=multiprocessing.get_context("forkserver")
            ) as pool:
                list(pool.map(noop_arg, range(count)))
                create = [micros(started) // count] * count
                extra = rss_tree() - base
                samples = latency(lambda: pool.submit(noop).result())
                begin = time.monotonic_ns()
                spans = list(pool.map(spin, [size] * count))
                wall = micros(begin)
    return {
        "kind": "fanout",
        "mode": kind,
        "count": count,
        "size": size,
        "create_us": create,
        "rss_kib": extra,
        "base_rss_kib": base,
        "latency_p50_us": percentile(samples, 50) if samples else 0,
        "latency_p99_us": percentile(samples, 99) if samples else 0,
        "single_ns": int(single["end"]) - int(single["start"]),
        "spans": spans,
        "wall_us": wall,
        "vm_pid": os.getpid(),
    }


def noop_arg(_index: int) -> int:
    return noop()


def crash_prefork(count: int) -> dict[str, object]:
    with ProcessPoolExecutor(count, mp_context=multiprocessing.get_context("forkserver")) as pool:
        list(pool.map(noop_arg, range(count)))
        victim = pool.submit(crash)
        others = [pool.submit(spin, 2_000_000) for _ in range(count - 1)]
        failed = 0
        for future in [victim, *others]:
            try:
                future.result()
            except BrokenProcessPool:
                failed += 1
        started = time.monotonic_ns()
    with ProcessPoolExecutor(count, mp_context=multiprocessing.get_context("forkserver")) as pool:
        list(pool.map(noop_arg, range(count)))
        replacement = micros(started)
    return {
        "kind": "crash",
        "mode": "prefork",
        "count": count,
        "others_alive": count - failed,
        "victim_result": "BrokenProcessPool: every pending job failed",
        "replacement_us": replacement,
    }


def crash_spawn(count: int) -> dict[str, object]:
    context = multiprocessing.get_context("spawn")
    victim = context.Process(target=crash)
    others = [context.Process(target=spin, args=(2_000_000,)) for _ in range(count - 1)]
    for process in [victim, *others]:
        process.start()
    for process in [victim, *others]:
        process.join()
    started = time.monotonic_ns()
    replacement = context.Process(target=noop)
    replacement.start()
    replacement.join()
    return {
        "kind": "crash",
        "mode": "spawn",
        "count": count,
        "others_alive": sum(process.exitcode == 0 for process in others),
        "victim_result": "exit code {}".format(victim.exitcode),
        "replacement_us": micros(started),
    }


async def fanin(count: int) -> dict[str, object]:
    base = rss_tree()
    go = asyncio.Event()

    async def wait() -> int:
        await go.wait()
        return noop()

    started = time.monotonic_ns()
    tasks = [asyncio.create_task(wait()) for _ in range(count)]
    await asyncio.sleep(0)
    spawn = micros(started)
    per_task = (rss_tree() - base) * 1024 // count
    begin = time.monotonic_ns()
    go.set()
    await asyncio.gather(*tasks)
    return {
        "kind": "fanin",
        "mode": "asyncio",
        "count": count,
        "contexts": 1,
        "window": count,
        "spawn_us": spawn,
        "bytes_per_process": per_task,
        "wall_us": micros(begin),
        "rss_kib": rss_tree(),
    }


async def main(
    kind: Literal["fanout", "crash", "fanin"], mode: Baseline, count: int, size: int
) -> None:
    match kind, mode:
        case "crash", "prefork":
            result = await asyncio.to_thread(crash_prefork, count)
        case "crash", "spawn":
            result = await asyncio.to_thread(crash_spawn, count)
        case "crash", _:
            crash()
            return
        case "fanin", _:
            result = await fanin(count)
        case _:
            result = await fanout(mode, count, size)
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["fanout", "crash", "fanin"])
    parser.add_argument("mode", choices=["asyncio", "threads", "spawn", "prefork"])
    parser.add_argument("count", type=int)
    parser.add_argument("size", type=int)
    arguments = parser.parse_args()
    asyncio.run(main(arguments.kind, arguments.mode, arguments.count, arguments.size))
