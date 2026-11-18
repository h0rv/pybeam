import asyncio
import os
import sys
import time
from pathlib import Path

import pybeam
from pybeam.codec import Json
from pybeam.queues import DurableTasks

try:
    import erlang
except ModuleNotFoundError as error:
    if error.name != "erlang":
        raise
    erlang = None

_shutdown = asyncio.Event()
_stopped = asyncio.Event()


def wait_release(path: Path) -> None:
    deadline = time.monotonic() + 30
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Task release marker did not arrive")
        time.sleep(0.02)


async def write_report(payload: bytes) -> bytes:
    if erlang is None:
        raise RuntimeError("Cluster body requires the managed runtime")
    assert write_report.__module__ == "__main__"
    await erlang.async_call("durable_body_started", payload)
    destination = Path(payload.decode())
    await asyncio.to_thread(wait_release, destination.with_suffix(".release"))
    await asyncio.to_thread(destination.write_text, "__main__ " + str(os.getpid()))
    return b""


async def cluster_submit(replicas: list[str], directory: str, start: int, count: int) -> bool:
    tasks = DurableTasks("cluster", replicas=replicas)
    for i in range(start, start + count):
        assert await tasks.submit(write_report, str(Path(directory) / str(i)).encode(), key=str(i))
    return True


async def cluster_stop() -> bool:
    _shutdown.set()
    await _stopped.wait()
    return True


async def main() -> None:
    if erlang is None:
        raise RuntimeError("Cluster serving requires the managed runtime")
    replicas = Json(list[str]).decode(sys.argv[1].encode())
    try:
        async with DurableTasks("cluster", replicas=replicas).serve(concurrency=2, lease=1.0):
            await erlang.async_call("durable_node_ready")
            await _shutdown.wait()
    finally:
        _stopped.set()


if __name__ == "__main__":
    pybeam.run(main)
