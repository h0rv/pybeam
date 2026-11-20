import asyncio
import sys
from pathlib import Path

from failure_workload import mark

import pybeam
from pybeam.queues import DurableTasks


async def complete(payload: bytes) -> bytes:
    await asyncio.sleep(0.5)
    await asyncio.to_thread(mark, Path("markers"), int(payload))
    return b""


async def main() -> None:
    tasks = DurableTasks("failure-probe")
    if sys.argv[1] == "submit":
        for task_id in range(20):
            if not await tasks.submit(complete, str(task_id).encode(), key=str(task_id)):
                raise RuntimeError("Unexpected duplicate submit")
        await asyncio.to_thread(Path("submitted").touch)
    async with tasks.serve(concurrency=1, lease=2.0):
        await asyncio.to_thread(Path("ready").touch)
        await asyncio.Event().wait()


if __name__ == "__main__":
    pybeam.run(main)
