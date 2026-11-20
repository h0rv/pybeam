# Rerun: ~/painpoint-venv/bin/python docs/research/probes/painpoints/uvicorn_faults.py crash|hog|runaway|state
import argparse
import asyncio
import ctypes
import json
import math
import os
import signal
import socket
import sys
import tempfile
import time
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

app = FastAPI()
subscribers: set[WebSocket] = set()


@dataclass
class State:
    count: int = 0


state = State()


@app.websocket("/ws")
async def endpoint(ws: WebSocket) -> None:
    await ws.accept()
    await ws.send_text(str(os.getpid()))
    try:
        while True:
            command = await ws.receive_text()
            if command == "crash":
                ctypes.string_at(0)
            elif command in {"hog", "runaway"}:
                await ws.send_text("started")
                deadline = time.monotonic() + (3 if command == "hog" else 60)
                value = 0
                while time.monotonic() < deadline:
                    value = (value * 31 + 1) & 0xFFFFFFFF
                await ws.send_text("done")
            elif command == "subscribe":
                subscribers.add(ws)
                await ws.send_text("ok")
            elif command == "publish":
                for subscriber in tuple(subscribers):
                    await subscriber.send_text("update")
                await ws.send_text("ok")
            elif command == "incr":
                state.count += 1
                await ws.send_text(str(state.count))
            else:
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        subscribers.discard(ws)


async def ready(url: str) -> None:
    workers: set[str] = set()
    for _ in range(300):
        try:
            async with connect(url, open_timeout=1) as ws:
                workers.add(str(await ws.recv()))
                if len(workers) == 4:
                    return
                await asyncio.sleep(0.05)
        except OSError, TimeoutError:
            await asyncio.sleep(0.1)
    raise TimeoutError("Server did not start")


async def trial(case: Literal["crash", "hog", "runaway", "state"]) -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    module = "uvicorn_faults:app"
    address = "127.0.0.1:" + str(port)
    command = [sys.executable, "-m", "uvicorn", module, "--workers", "4", "--port", str(port)]
    if case == "runaway":
        command = [
            sys.executable,
            "-m",
            "gunicorn",
            module,
            "--workers",
            "4",
            "--worker-class",
            "uvicorn_worker.UvicornWorker",
            "--timeout",
            "10",
            "--bind",
            address,
        ]
    with tempfile.TemporaryFile() as log:
        process = await asyncio.create_subprocess_exec(
            *command, cwd=Path(__file__).parent, stdout=log, stderr=log, start_new_session=True
        )
        try:
            url = "ws://" + address + "/ws"
            await ready(url)
            async with AsyncExitStack() as stack:
                clients: list[ClientConnection] = []
                pids: list[int] = []
                for _ in range(8 if case == "state" else 20):
                    ws = await stack.enter_async_context(connect(url, ping_interval=None))
                    clients.append(ws)
                    pids.append(int(str(await ws.recv())))
                fault = await stack.enter_async_context(connect(url, ping_interval=None))
                fault_pid = int(str(await fault.recv()))
                distribution = dict(Counter(pids))
                if case == "state":
                    for ws in clients:
                        await ws.send("subscribe")
                        if await ws.recv() != "ok":
                            raise RuntimeError("Subscription failed")
                    values: list[int] = []
                    for _ in range(400):
                        async with connect(url) as writer:
                            await writer.recv()
                            await writer.send("incr")
                            values.append(int(str(await writer.recv())))
                    await fault.send("publish")
                    if await fault.recv() != "ok":
                        raise RuntimeError("Publish failed")

                    async def notified(ws: ClientConnection) -> bool:
                        try:
                            return await asyncio.wait_for(ws.recv(), 0.5) == "update"
                        except TimeoutError:
                            return False

                    async with asyncio.TaskGroup() as group:
                        notices = [group.create_task(notified(ws)) for ws in clients]
                    print(
                        json.dumps(
                            {
                                "case": case,
                                "workers": distribution,
                                "publisher_pid": fault_pid,
                                "highest_count": max(values),
                                "increments": 400,
                                "notified": sum(task.result() for task in notices),
                                "subscribers": 8,
                            }
                        )
                    )
                    return
                latencies: list[float] = []
                worst: list[float] = []
                dropped: list[int] = []
                started = asyncio.Event()
                fault_at = 0.0
                killed_after: float | None = None

                async def watch(index: int, ws: ClientConnection) -> None:
                    await started.wait()
                    maximum = 0.0
                    end = time.monotonic() + (16 if case == "runaway" else 5)
                    try:
                        while time.monotonic() < end:
                            sent = time.monotonic()
                            await ws.send("ping")
                            await asyncio.wait_for(ws.recv(), 20)
                            elapsed = time.monotonic() - sent
                            latencies.append(elapsed)
                            maximum = max(maximum, elapsed)
                            await asyncio.sleep(0.02)
                    except ConnectionClosed:
                        dropped.append(index)
                    finally:
                        worst.append(maximum)

                async def trigger() -> None:
                    nonlocal fault_at, killed_after
                    fault_at = time.monotonic()
                    await fault.send(case)
                    if case != "crash" and await fault.recv() != "started":
                        raise RuntimeError("Fault did not start")
                    started.set()
                    try:
                        await fault.recv()
                    except ConnectionClosed:
                        killed_after = time.monotonic() - fault_at

                async with asyncio.timeout(30), asyncio.TaskGroup() as group:
                    for index, ws in enumerate(clients):
                        group.create_task(watch(index, ws))
                    group.create_task(trigger())
                ordered = sorted(latencies)
                log.seek(0)
                logs = log.read().decode(errors="replace")
                if case == "runaway" and "WORKER TIMEOUT" not in logs:
                    raise RuntimeError("Worker did not hit Gunicorn timeout: " + logs)
                print(
                    json.dumps(
                        {
                            "case": case,
                            "workers": distribution,
                            "fault_pid": fault_pid,
                            "clients_on_fault_worker": pids.count(fault_pid),
                            "dropped": len(dropped),
                            "dropped_pids": [pids[index] for index in dropped],
                            "max_s": max(ordered, default=0),
                            "p99_s": ordered[math.ceil(len(ordered) * 0.99) - 1]
                            if ordered
                            else None,
                            "clients_waited_over_1s": sum(value > 1 for value in worst),
                            "fault_disconnect_s": killed_after,
                            "gunicorn_timeout_confirmed": "WORKER TIMEOUT" in logs,
                        }
                    )
                )
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=("crash", "hog", "runaway", "state"))
    argument = parser.parse_args().case
    match argument:
        case "crash" | "hog" | "runaway" | "state":
            asyncio.run(trial(argument))
