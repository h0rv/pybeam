# Rerun: uv run --locked python docs/research/probes/painpoints/loop_stall_pybeam.py
import asyncio
import json
import math
import os
import signal
import sys

import loop_stall_pybeam as entries
from loop_stall import CLIENTS, client, report_card

from pybeam import run
from pybeam.codec import Json
from pybeam.tasks import ExecutionMode, TaskFunction, TaskLimits, TaskSupervisor


async def report(payload: bytes) -> bytes:
    return Json(int).encode(report_card(Json(int).decode(payload)))


async def one_report(port: int) -> None:
    await asyncio.sleep(1)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"report\n")
        await writer.drain()
        if await reader.readline() != b"report\n":
            raise RuntimeError("Report failed before returning its result")
    finally:
        writer.close()
        await writer.wait_closed()


async def serve() -> None:
    job = TaskFunction(entries.report, Json(int), Json(int), execution=ExecutionMode.ISOLATED)
    async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=3000)) as tasks:

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while line := await reader.readline():
                    if line == b"report\n":
                        await (await tasks.start(job, 40_000_000))
                    writer.write(line)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 8767)
        print("ready", flush=True)
        async with server:
            await server.serve_forever()


async def main() -> None:
    server = await asyncio.create_subprocess_exec(
        sys.executable, __file__, "serve", stdout=asyncio.subprocess.PIPE, start_new_session=True
    )
    try:
        if server.stdout is None or await server.stdout.readline() != b"ready\n":
            raise RuntimeError("Server failed to start")
        latencies: list[float] = []
        worst: list[float] = []
        async with asyncio.timeout(30), asyncio.TaskGroup() as group:
            for _ in range(CLIENTS):
                group.create_task(client(8767, latencies, worst))
            group.create_task(one_report(8767))
        ordered = sorted(latencies)
        print(
            json.dumps(
                {
                    "case": "hog",
                    "max_s": max(latencies) / 1000,
                    "p99_s": ordered[math.ceil(len(ordered) * 0.99) - 1] / 1000,
                    "clients_waited_over_1s": sum(value > 1000 for value in worst),
                    "clients": CLIENTS,
                }
            )
        )
    finally:
        if server.returncode is None:
            os.killpg(server.pid, signal.SIGTERM)
        await server.wait()


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        run(serve)
    else:
        asyncio.run(main())
