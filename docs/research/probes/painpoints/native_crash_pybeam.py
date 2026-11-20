# Rerun: uv run --locked python docs/research/probes/painpoints/native_crash_pybeam.py
import asyncio
import ctypes
import json
import os
import signal
import sys

import native_crash_pybeam as entries
from native_crash import CLIENTS, bad_upload, user

from pybeam import run
from pybeam.codec import Json
from pybeam.tasks import ExecutionMode, TaskCrashed, TaskFunction, TaskLimits, TaskSupervisor


async def parse_upload(payload: bytes) -> bytes:
    ctypes.string_at(0)
    return payload


async def serve() -> None:
    job = TaskFunction(
        entries.parse_upload, Json(bytes), Json(bytes), execution=ExecutionMode.ISOLATED
    )
    async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=3000)) as tasks:

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while line := await reader.readline():
                    if line == b"parse-upload\n":
                        try:
                            await (await tasks.start(job, b"upload"))
                        except TaskCrashed as error:
                            if error.reason != ("signal", signal.SIGSEGV):
                                raise RuntimeError(
                                    "Unexpected child failure: " + repr(error.reason)
                                ) from error
                            print("isolated task crashed:", error.reason, flush=True)
                            return
                        raise RuntimeError("Expected isolated native crash")
                    writer.write(line)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 8765)
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
        alive = [True] * CLIENTS
        async with asyncio.timeout(30), asyncio.TaskGroup() as group:
            for index in range(CLIENTS):
                group.create_task(user(alive, index))
            group.create_task(bad_upload())
        async with asyncio.timeout(5):
            while outcome := await server.stdout.readline():
                if outcome.startswith(b"isolated task crashed:"):
                    break
            else:
                raise RuntimeError("Server exited without observing the isolated crash")
            reader, writer = await asyncio.open_connection("127.0.0.1", 8765)
            try:
                writer.write(b"ping\n")
                await writer.drain()
                if await reader.readline() != b"ping\n":
                    raise RuntimeError("Server did not answer after the crash")
            finally:
                writer.close()
                await writer.wait_closed()
        print(
            json.dumps(
                {
                    "case": "crash",
                    "dropped": alive.count(False),
                    "clients": CLIENTS,
                    "outcome": outcome.decode().strip(),
                    "server_running": server.returncode is None,
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
