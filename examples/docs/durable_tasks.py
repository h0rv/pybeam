# durable_tasks.py
import asyncio

from durable_body import write_report

import pybeam
from pybeam.queues import DurableTasks


async def main() -> None:
    tasks = DurableTasks("reports")
    async with tasks.serve(concurrency=2, lease=60.0):
        await tasks.submit(write_report, b"Report ready\n", key="report:42")
        print("Task recorded; workers running.", flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    pybeam.run(main)
