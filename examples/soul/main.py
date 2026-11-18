# main.py
import asyncio
import os
import socket
import statistics
import time
from dataclasses import dataclass, field
from http.client import HTTPConnection
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI

import pybeam
from examples.soul.entries import formula_sum, iterative_sum
from pybeam.actors import ActorGroup, actor
from pybeam.codec import Json
from pybeam.tasks import ExecutionMode, TaskCrashed, TaskFunction, TaskLimits, TaskSupervisor


@actor
class Requests:
    completed: int = 0

    async def record(self) -> int:
        self.completed += 1
        return self.completed

    async def read(self) -> int:
        return self.completed


app = FastAPI()


async def current_requests() -> Requests:
    raise RuntimeError("The request actor is not running")


@app.get("/")
async def respond(requests: Annotated[Requests, Depends(current_requests)]) -> int:
    return await requests.record()


@dataclass(slots=True)
class Traffic:
    latencies: list[float] = field(default_factory=list)
    failures: int = 0
    stopped: asyncio.Event = field(default_factory=asyncio.Event)

    async def client(self, url: str) -> None:
        while not self.stopped.is_set() and len(self.latencies) < 1000:
            started = time.monotonic()
            try:
                await asyncio.to_thread(read_response, url)
            except OSError as error:
                self.failures += 1
                print("HTTP client failed: " + str(error), flush=True)
            else:
                self.latencies.append((time.monotonic() - started) * 1000)
            await asyncio.sleep(0.02)

    async def sample(self, label: str) -> None:
        before = len(self.latencies)
        await asyncio.sleep(0.3)
        values = self.latencies[before:]
        if not values:
            raise RuntimeError("No successful HTTP responses during " + label)
        print(
            "{}: {} HTTP responses; median {:.1f} ms; max {:.1f} ms; {} failures".format(
                label, len(values), statistics.median(values), max(values), self.failures
            ),
            flush=True,
        )


def read_response(address: str) -> bytes:
    connection = HTTPConnection(address, timeout=2)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        if response.status != 200:
            raise OSError("HTTP request failed", response.status)
        return response.read()
    finally:
        connection.close()


async def walkthrough(requests: Requests, actors: ActorGroup, address: str) -> None:
    vm_pid = os.getpid()
    before = await actors.info(requests)
    traffic = Traffic()
    function = TaskFunction(iterative_sum, Json(int), Json(int), execution=ExecutionMode.ISOLATED)
    print("PyBEAM: inspect, stop, replace — while HTTP keeps serving", flush=True)
    print("VM PID " + str(vm_pid) + "; request actor " + before.pid, flush=True)
    async with (
        TaskSupervisor(TaskLimits(capacity=2, max_bytes=4096, shutdown_ms=5000)) as tasks,
        asyncio.TaskGroup() as clients,
    ):
        clients.create_task(traffic.client(address))
        clients.create_task(traffic.client(address))
        try:
            await traffic.sample("Before the bug")
            stuck = await tasks.start(function, -1)
            try:
                first = await stuck.inspect()
                await traffic.sample("Sum(-1) does not yield")
                second = await stuck.inspect()
                if first.os_pid != second.os_pid:
                    raise RuntimeError("The inspected child changed between samples")
                print(
                    "Child PID {}; CPU used between samples {:.3f} seconds".format(
                        second.os_pid, second.cpu_seconds - first.cpu_seconds
                    ),
                    flush=True,
                )
                print(second.stacks, flush=True)
            finally:
                await stuck.terminate()
            error = await stuck.exception()
            if not isinstance(error, TaskCrashed):
                raise RuntimeError("Expected the busy child to exit", error)
            print("Stopped child; task outcome " + str(error.reason), flush=True)
            await traffic.sample("After stopping only the calculation")

            function = TaskFunction(
                formula_sum, Json(int), Json(int), execution=ExecutionMode.ISOLATED
            )
            upto = 1_000_000_000
            result = await (await tasks.start(function, upto))
            if result != upto * (upto + 1) // 2:
                raise RuntimeError("Incorrect replacement result")
            rejected = await tasks.start(function, -1)
            if await rejected.exception() is None:
                raise RuntimeError("The replacement accepted a negative bound")
            print("Replacement sum(1,000,000,000) = " + str(result), flush=True)
            print("Replacement rejects negative inputs; no job was retried", flush=True)
            await traffic.sample("After replacing the entry for new calculations")
        finally:
            traffic.stopped.set()

    after = await actors.info(requests)
    completed = await requests.read()
    if (before.pid, before.generation) != (after.pid, after.generation) or os.getpid() != vm_pid:
        raise RuntimeError("The serving VM or request actor restarted")
    if traffic.failures:
        raise RuntimeError("HTTP failures observed", traffic.failures)
    if completed != len(traffic.latencies):
        raise RuntimeError("The actor count does not match successful responses")
    print(
        "{} requests counted by the same actor, generation {}; VM PID unchanged".format(
            completed, after.generation
        ),
        flush=True,
    )
    print(
        "Boundary: OS child isolation, not BEAM preemption of Python. "
        "The fix selects a new entry; it does not upgrade running Python frames.",
        flush=True,
    )


async def main() -> None:
    async with ActorGroup(max_children=1) as actors:
        requests = await actors.spawn(Requests)

        async def get_requests() -> Requests:
            return requests

        app.dependency_overrides[current_requests] = get_requests

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            listener.setblocking(False)
            config = uvicorn.Config(
                app, log_level="error", access_log=False, loop="none", http="h11"
            )
            server = uvicorn.Server(config)
            async with asyncio.TaskGroup() as group:
                serving = group.create_task(server.serve(sockets=[listener]))
                try:
                    async with asyncio.timeout(5):
                        while not server.started:
                            if serving.done():
                                serving.result()
                                raise RuntimeError("HTTP server exited before startup")
                            await asyncio.sleep(0.01)
                    await walkthrough(
                        requests, actors, "127.0.0.1:" + str(listener.getsockname()[1])
                    )
                finally:
                    server.should_exit = True
                    app.dependency_overrides.clear()


if __name__ == "__main__":
    pybeam.run(main)
