# durable_tasks.py
import asyncio
from pathlib import Path

import pybeam
from pybeam.queues import DurableTasks


async def write_report(payload: bytes) -> bytes:
    await asyncio.to_thread(Path("report.txt").write_bytes, payload)
    return b""


async def main() -> None:
    tasks = DurableTasks("reports")
    async with tasks.serve(concurrency=2, lease=60.0):
        await tasks.submit(write_report, b"Report ready\n", key="report:42")
        print("Task recorded; workers running.", flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    pybeam.run(main)
