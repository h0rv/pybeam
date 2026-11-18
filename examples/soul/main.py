# main.py
import asyncio
import logging
import os
import re
import socket
import statistics
import time
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.client import HTTPConnection
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import pybeam
from examples.soul.entries import formula_sum, iterative_sum
from pybeam.actors import ActorGroup, actor
from pybeam.codec import Json
from pybeam.tasks import ExecutionMode, TaskCrashed, TaskFunction, TaskLimits, TaskSupervisor

HERE = Path(__file__).parent
type Phase = Literal[
    "waiting", "baseline", "running", "found", "stopped", "replaced", "complete", "failed"
]


@actor
class Requests:
    completed: int = 0

    async def record(self) -> int:
        self.completed += 1
        return self.completed

    async def read(self) -> int:
        return self.completed


@dataclass(frozen=True, slots=True)
class Point:
    seconds: float
    milliseconds: float


@dataclass(frozen=True, slots=True)
class Event:
    seconds: float
    label: str


@dataclass(frozen=True, slots=True)
class Frame:
    location: str
    source: str


def user_frame(stacks: str) -> Frame:
    lines = (HERE / "entries.py").read_text().splitlines()
    for filename, number, function in re.findall(
        r'File "([^"]+)", line (\d+), in ([^\n]+)', stacks
    ):
        if Path(filename).resolve() == (HERE / "entries.py").resolve():
            line = int(number)
            return Frame("entries.py:{} in {}".format(line, function), lines[line - 1].strip())
    raise RuntimeError("The calculation's frame was not found")


@dataclass(slots=True)
class Traffic:
    samples: deque[Point] = field(default_factory=lambda: deque(maxlen=2048))
    completed: int = 0
    failures: int = 0
    started: float = field(default_factory=time.monotonic)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)

    async def client(self, address: str) -> None:
        while not self.stopped.is_set():
            started = time.monotonic()
            try:
                await asyncio.to_thread(read_response, address)
            except OSError:
                self.failures += 1
            else:
                if len(self.samples) == self.samples.maxlen:
                    raise RuntimeError("The latency sample buffer is full")
                self.completed += 1
                self.samples.append(
                    Point(time.monotonic() - self.started, (time.monotonic() - started) * 1000)
                )
            await asyncio.sleep(0.02)

    async def sample(self, seconds: float = 2) -> None:
        before = self.completed
        await asyncio.sleep(seconds)
        if self.completed == before:
            raise RuntimeError("HTTP traffic stopped")


def read_response(address: str) -> bytes:
    connection = HTTPConnection(address, timeout=2)
    try:
        connection.request("GET", "/request")
        response = connection.getresponse()
        if response.status != 200:
            raise OSError("HTTP request failed", response.status)
        return response.read()
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class View:
    phase: Phase
    samples: tuple[Point, ...]
    events: tuple[Event, ...]
    completed: int
    failures: int
    median_ms: float
    max_ms: float
    frame: Frame | None
    cpu_seconds: float
    result: str
    error: str


@dataclass(slots=True)
class Demo:
    requests: Requests
    actors: ActorGroup
    address: str
    phase: Phase = "waiting"
    traffic: Traffic = field(default_factory=Traffic)
    events: list[Event] = field(default_factory=list)
    frame: Frame | None = None
    cpu_seconds: float = 0
    result: str = ""
    error: str = ""
    requested: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    def begin(self) -> None:
        if self.phase != "waiting":
            raise HTTPException(409, "The walkthrough has already started")
        self.phase = "baseline"
        self.requested.set()

    def mark(self, phase: Phase, label: str) -> None:
        self.phase = phase
        self.events.append(Event(time.monotonic() - self.traffic.started, label))

    def view(self) -> View:
        samples = tuple(self.traffic.samples)
        values = tuple(point.milliseconds for point in samples)
        return View(
            self.phase,
            samples,
            tuple(self.events),
            self.traffic.completed,
            self.traffic.failures,
            statistics.median(values) if values else 0,
            max(values, default=0),
            self.frame,
            self.cpu_seconds,
            self.result,
            self.error,
        )

    async def run(self) -> None:
        await self.requested.wait()
        try:
            await self.walkthrough()
        except asyncio.CancelledError:
            self.phase = "failed"
            self.error = "Walkthrough interrupted; no final outcome confirmed"
            raise
        except Exception:
            self.phase = "failed"
            self.error = "Verification failed; see the server log"
            logging.exception("Walkthrough failed")
        finally:
            self.finished.set()

    async def walkthrough(self) -> None:
        vm_pid = os.getpid()
        before = await self.actors.info(self.requests)
        self.traffic.started = time.monotonic()
        function = TaskFunction(
            iterative_sum, Json(int), Json(int), execution=ExecutionMode.ISOLATED
        )
        async with (
            TaskSupervisor(TaskLimits(capacity=2, max_bytes=4096, shutdown_ms=5000)) as tasks,
            asyncio.TaskGroup() as clients,
        ):
            clients.create_task(self.traffic.client(self.address))
            clients.create_task(self.traffic.client(self.address))
            try:
                await self.traffic.sample()
                stuck = await tasks.start(function, -1)
                self.mark("running", "Started sum(-1)")
                try:
                    first = await stuck.inspect()
                    await self.traffic.sample(3)
                    second = await stuck.inspect()
                    if first.os_pid != second.os_pid:
                        raise RuntimeError("The calculation changed between samples")
                    self.frame = user_frame(second.stacks)
                    self.cpu_seconds = second.cpu_seconds - first.cpu_seconds
                    if self.cpu_seconds <= 0:
                        raise RuntimeError("The calculation did not consume CPU")
                    self.mark("found", "Found the non-yielding loop")
                    await self.traffic.sample()
                finally:
                    await stuck.terminate()
                error = await stuck.exception()
                if not isinstance(error, TaskCrashed) or error.reason != ("signal", 9):
                    raise RuntimeError("Calculation exit was not confirmed")
                self.mark("stopped", "Stopped only the calculation")
                await self.traffic.sample()
                function = TaskFunction(
                    formula_sum, Json(int), Json(int), execution=ExecutionMode.ISOLATED
                )
                result = await (await tasks.start(function, 1_000_000_000))
                if result != 1_000_000_000 * 1_000_000_001 // 2:
                    raise RuntimeError("Incorrect replacement result")
                rejected = await tasks.start(function, -1)
                rejection = await rejected.exception()
                if rejection is None or "The upper bound must be nonnegative" not in str(rejection):
                    raise RuntimeError("The replacement did not reject negative input")
                self.result = format(result, ",")
                self.mark("replaced", "Selected the corrected entry")
                await self.traffic.sample()
            finally:
                self.traffic.stopped.set()
        after = await self.actors.info(self.requests)
        if (before.pid, before.generation, vm_pid) != (after.pid, after.generation, os.getpid()):
            raise RuntimeError("The serving process restarted")
        if self.traffic.failures or await self.requests.read() != self.traffic.completed:
            raise RuntimeError("HTTP responses and counter state do not match")
        self.phase = "complete"


app = FastAPI()
templates = Jinja2Templates(directory=HERE)
app.mount("/assets", StaticFiles(directory=HERE / "static"), name="assets")


async def current_demo() -> Demo:
    raise RuntimeError("The walkthrough server is not running")


type LiveDemo = Annotated[Demo, Depends(current_demo)]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/request")
async def respond(demo: LiveDemo) -> int:
    return await demo.requests.record()


@app.get("/state")
async def state(demo: LiveDemo) -> View:
    return demo.view()


@app.post("/run", status_code=202)
async def begin(demo: LiveDemo) -> View:
    demo.begin()
    return demo.view()


@asynccontextmanager
async def live_view(port: int) -> AsyncGenerator[Demo]:
    async with ActorGroup(max_children=1) as actors:
        requests = await actors.spawn(Requests)
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
            listener.listen(128)
            listener.setblocking(False)
            demo = Demo(requests, actors, "127.0.0.1:" + str(listener.getsockname()[1]))

            async def get_demo() -> Demo:
                return demo

            app.dependency_overrides[current_demo] = get_demo
            config = uvicorn.Config(
                app, log_level="error", access_log=False, loop="none", http="h11"
            )
            server = uvicorn.Server(config)
            try:
                async with asyncio.TaskGroup() as group:
                    serving = group.create_task(server.serve(sockets=[listener]))
                    running = group.create_task(demo.run())
                    try:
                        async with asyncio.timeout(5):
                            while not server.started:
                                if serving.done():
                                    serving.result()
                                    raise RuntimeError("HTTP server exited before startup")
                                await asyncio.sleep(0.01)
                        yield demo
                    finally:
                        running.cancel()
                        server.should_exit = True
                        await serving
            finally:
                app.dependency_overrides.clear()


async def main() -> None:
    async with live_view(8765):
        print("Open http://127.0.0.1:8765", flush=True)
        await asyncio.Future[None]()


if __name__ == "__main__":
    os.environ["ERL_FLAGS"] = os.environ.get("ERL_FLAGS", "") + " -kernel logger_level emergency"
    pybeam.run(main)
