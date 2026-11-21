import asyncio
import os
import sys
import time
from pathlib import Path

import pybeam
from pybeam.queues import DurableTasks
from pybeam.runtime.ownership import durable_write
from tests.fixtures.durable_tasks_body import cluster_complete, complete

try:
    import erlang
except ModuleNotFoundError as error:
    if error.name != "erlang":
        raise
    erlang = None

_cluster_shutdown = asyncio.Event()
_cluster_stopped = asyncio.Event()


def wait_completed(directory: Path, count: int) -> None:
    deadline = time.monotonic() + 20
    while not all((directory / str(i)).is_file() for i in range(count)):
        if time.monotonic() >= deadline:
            raise TimeoutError("Durable tasks did not complete")
        time.sleep(0.01)


async def main() -> None:
    mode = sys.argv[1]
    count = 12
    directory = await asyncio.to_thread(
        Path("completed-" + ("proof" if mode == "proof" else "recovery")).resolve
    )
    await asyncio.to_thread(directory.mkdir, exist_ok=True)
    tasks = DurableTasks("proof" if mode == "proof" else "recovery")
    if mode == "proof":
        async with tasks.serve(concurrency=2):
            first = str(directory / "0").encode()
            assert await tasks.submit(complete, first, key="0")
            assert not await tasks.submit(complete, first, key="0")
            for i in range(1, count):
                assert await tasks.submit(complete, str(directory / str(i)).encode(), key=str(i))
            await asyncio.to_thread(wait_completed, directory, count)
        print("TASKS_COMPLETED single_node", flush=True)
    elif mode == "put":
        for i in range(count):
            assert await tasks.submit(complete, str(directory / str(i)).encode(), key=str(i))
        await asyncio.to_thread(durable_write, Path("stored.pid"), str(os.getpid()))
        await asyncio.Event().wait()
    else:
        async with tasks.serve(concurrency=3):
            await asyncio.to_thread(wait_completed, directory, count)
        print("TASKS_RECOVERED acknowledged_submits", flush=True)


async def cluster_serve(replicas: list[str]) -> bool:
    if erlang is None:
        raise RuntimeError("Cluster serving requires the managed runtime")
    try:
        async with DurableTasks("cluster", replicas=replicas).serve(concurrency=2, lease=1.0):
            await erlang.async_call("durable_node_ready")
            await _cluster_shutdown.wait()
    finally:
        _cluster_stopped.set()
    return True


async def cluster_stop() -> bool:
    _cluster_shutdown.set()
    await _cluster_stopped.wait()
    return True


async def cluster_submit(replicas: list[str], directory: str, start: int, count: int) -> bool:
    tasks = DurableTasks("cluster", replicas=replicas)
    for i in range(start, start + count):
        assert await tasks.submit(
            cluster_complete, str(Path(directory) / str(i)).encode(), key=str(i)
        )
    return True


if __name__ == "__main__":
    pybeam.run(main)
