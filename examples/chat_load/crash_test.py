import argparse
import asyncio
import math
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import httpx
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from examples.chat_load.faults import FaultKind
from examples.chat_load.origin import require_same_origin
from examples.chat_load.protocol import Ack, Fault, Push, Send
from examples.chat_load.traffic import summarize

type SideId = Literal["usual", "pybeam"]
type Status = Literal[
    "healthy",
    "injecting",
    "contained",
    "completed",
    "failed",
    "disconnected",
    "reconnecting",
    "unknown",
]

HERE = Path(__file__).parent
PUSH: TypeAdapter[Push] = TypeAdapter(Push)
BASELINE_SECONDS = 8.0
OBSERVE_SECONDS = 10.0
RECOVERY_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Step:
    kind: FaultKind
    title: str
    simulates: str
    expected: str


STEPS = (
    Step(
        "raise",
        "A handler raises",
        "A bug in one room's handler raises an exception.",
        "Usual returns HTTP 500; PyBeam contains and reports the failure. Other rooms should be unaffected on both.",
    ),
    Step(
        "hog",
        "A room hogs the CPU",
        "One room runs 3 seconds of CPU-bound work.",
        "The usual worker freezes, stalling other rooms on that worker for seconds. PyBeam runs the work separately; other rooms should stay at millisecond latency.",
    ),
    Step(
        "abort",
        "A native process crashes",
        "Native code in one room crashes its process: os.abort(), like a segfault in a C extension.",
        "The usual worker process dies and its sockets in other rooms disconnect. PyBeam keeps the crash in an isolated child process.",
    ),
)


@dataclass(slots=True)
class Measurement:
    p99: float | None = None
    pending_ms: float = 0
    dropped: int = 0
    lost: int = 0


@dataclass(slots=True)
class Observation:
    clients: list[tuple[Client, int, int]]
    measurement: Measurement = field(default_factory=Measurement)

    def update(self, rooms: list[Room], now: float) -> None:
        for room in rooms:
            if room.p99 is not None:
                previous = self.measurement.p99
                self.measurement.p99 = max(previous or 0, room.p99)
            self.measurement.pending_ms = max(self.measurement.pending_ms, room.oldest_ms(now))
        self.measurement.dropped = sum(client.drops > before for client, before, _ in self.clients)
        self.measurement.lost = sum(client.lost - before for client, _, before in self.clients)


@dataclass(frozen=True, slots=True)
class Result:
    step: Step
    measurements: dict[SideId, Measurement]
    outcomes: dict[SideId, str]


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    usual_url: HttpUrl = HttpUrl("http://127.0.0.1:8801")
    pybeam_url: HttpUrl = HttpUrl("http://127.0.0.1:8800")
    rooms: int = Field(default=8, ge=1, le=32)
    clients: int = Field(default=4, ge=1, le=16)
    rate: float = Field(default=4, ge=0.1, le=20)
    port: int = Field(default=8802, ge=1024, le=65535)


@dataclass(slots=True)
class Client:
    name: str
    state: Literal["connected", "disconnected", "reconnecting"] = "reconnecting"
    pending: dict[int, float] = field(default_factory=dict)
    reconnects: int = 0
    lost: int = 0
    drops: int = 0
    error: str = ""


@dataclass(slots=True)
class Room:
    name: str
    clients: list[Client]
    samples: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=4096))
    history: deque[float | None] = field(default_factory=lambda: deque(maxlen=40))
    p99: float | None = None
    sample_count: int = 0
    notice: Status = "healthy"
    notice_until: float = 0
    last_fault: str = "No fault injected"

    def mark(self, kind: FaultKind, outcome: Status, now: float) -> None:
        self.notice = outcome
        self.notice_until = now + 6
        self.last_fault = kind + " · " + outcome

    def sample(self, now: float) -> None:
        while self.samples and self.samples[0][0] < now - 2:
            self.samples.popleft()
        latency = summarize([value for _, value in self.samples])
        self.p99 = None if latency is None else latency.p99
        self.sample_count = len(self.samples)
        self.history.append(self.p99)

    @property
    def connected(self) -> int:
        return sum(client.state == "connected" for client in self.clients)

    def status(self, now: float) -> Status:
        if any(client.state == "disconnected" for client in self.clients):
            return "disconnected"
        if self.connected < len(self.clients):
            return "reconnecting"
        return self.notice if self.notice == "injecting" or now < self.notice_until else "healthy"

    @property
    def pending(self) -> int:
        return sum(len(client.pending) for client in self.clients)

    def oldest_ms(self, now: float) -> float:
        oldest = min(
            (started for client in self.clients for started in client.pending.values()),
            default=now,
        )
        return max(0, (now - oldest) * 1000)

    @property
    def reconnects(self) -> int:
        return sum(client.reconnects for client in self.clients)

    @property
    def lost(self) -> int:
        return sum(client.lost for client in self.clients)

    @property
    def lines(self) -> list[str]:
        lines: list[str] = []
        points: list[str] = []
        for index, value in enumerate(self.history):
            if value is None:
                if points:
                    lines.append(" ".join(points))
                    points = []
            else:
                points.append("{:.1f},{:.1f}".format(index * 3, 24 - min(value, 100) * 0.22))
        if points:
            lines.append(" ".join(points))
        return lines


@dataclass(slots=True)
class Side:
    key: SideId
    title: str
    url: str
    rooms: list[Room]
    running: bool = False
    result: str = "Ready · target room-1"


class Dashboard:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.group: asyncio.TaskGroup | None = None
        self.http: httpx.AsyncClient | None = None
        self.run_task: asyncio.Task[None] | None = None
        self.running = False
        self.phase: Literal["ready", "baseline", "observe", "recovery", "done", "interrupted"] = (
            "ready"
        )
        self.step_number = 0
        self.until = 0.0
        self.current: Step | None = None
        self.observations: dict[SideId, Observation] = {}
        self.results: list[Result] = []
        tag = uuid4().hex[:8]
        self.sides: dict[SideId, Side] = {}
        targets: tuple[tuple[SideId, str, HttpUrl], ...] = (
            ("usual", "Usual stack", settings.usual_url),
            ("pybeam", "PyBeam", settings.pybeam_url),
        )
        for key, title, url in targets:
            rooms = [
                Room(
                    "room-" + str(index + 1),
                    [Client("dash-" + tag + "-" + str(i)) for i in range(settings.clients)],
                )
                for index in range(settings.rooms)
            ]
            self.sides[key] = Side(key, title, str(url).rstrip("/"), rooms)

    async def send(self, socket: ClientConnection, client: Client) -> None:
        sequence = 0
        while True:
            now = time.monotonic()
            if len(client.pending) >= 64 or any(now - sent > 8 for sent in client.pending.values()):
                raise TimeoutError("Acknowledgements stalled")
            client.pending[sequence] = now
            await socket.send(Send(id=sequence, text="chat").model_dump_json())
            sequence += 1
            await asyncio.sleep(1 / self.settings.rate)

    def record_pending(self, side: Side, room: Room, now: float) -> None:
        if self.phase == "observe" and now <= self.until and room is not side.rooms[0]:
            measurement = self.observations[side.key].measurement
            measurement.pending_ms = max(measurement.pending_ms, room.oldest_ms(now))

    async def receive(
        self, socket: ClientConnection, side: Side, room: Room, client: Client
    ) -> None:
        while True:
            push = PUSH.validate_json(await socket.recv())
            now = time.monotonic()
            match push:
                case Ack(id=sequence):
                    self.record_pending(side, room, now)
                    started = client.pending.pop(sequence, None)
                    if started is not None:
                        room.samples.append((now, (now - started) * 1000))
                case Fault(kind=kind, outcome=outcome):
                    room.mark(kind, outcome, now)

    async def run_client(self, side: Side, room: Room, client: Client) -> None:
        url = (
            side.url.replace("http", "ws", 1) + "/rooms/" + room.name + "/ws?client=" + client.name
        )
        connected_before = False
        while True:
            opened = False
            try:
                async with connect(
                    url,
                    open_timeout=3,
                    close_timeout=1,
                    ping_interval=2,
                    ping_timeout=4,
                    max_size=65_536,
                    max_queue=64,
                ) as socket:
                    opened = True
                    if connected_before:
                        client.reconnects += 1
                    connected_before = True
                    client.state = "connected"
                    client.error = ""
                    async with asyncio.TaskGroup() as tasks:
                        tasks.create_task(self.send(socket, client))
                        tasks.create_task(self.receive(socket, side, room, client))
            except* (
                OSError,
                ConnectionClosed,
                InvalidHandshake,
                TimeoutError,
                ValidationError,
            ) as errors:
                client.error = str(errors.exceptions[0])
            finally:
                self.record_pending(side, room, time.monotonic())
                if opened:
                    client.drops += 1
                client.state = "disconnected"
                client.lost += len(client.pending)
                client.pending.clear()
            await asyncio.sleep(0.75)
            client.state = "reconnecting"
            await asyncio.sleep(0.75)

    def sample_once(self, now: float) -> None:
        for side in self.sides.values():
            for room in side.rooms:
                room.sample(now)
        if self.phase == "observe" and now <= self.until:
            for key, observation in self.observations.items():
                observation.update(self.sides[key].rooms[1:], now)

    async def sample(self) -> None:
        while True:
            self.sample_once(time.monotonic())
            await asyncio.sleep(0.5)

    def remaining(self, now: float) -> int:
        return math.ceil(max(0, self.until - now)) if self.running else 0

    def start_run(self) -> None:
        if self.running:
            raise HTTPException(409, "A crash test is already running")
        if self.group is None or self.http is None:
            raise HTTPException(503, "Scenario runner is starting")
        self.running = True
        self.phase = "baseline"
        self.step_number = 1
        self.until = time.monotonic() + BASELINE_SECONDS
        self.current = None
        self.observations.clear()
        self.results.clear()
        self.run_task = self.group.create_task(self.scenario(self.http))

    def total(self, key: SideId) -> Measurement:
        measurements = [result.measurements[key] for result in self.results]
        latencies = [item.p99 for item in measurements if item.p99 is not None]
        return Measurement(
            max(latencies, default=None),
            max((item.pending_ms for item in measurements), default=0),
            sum(item.dropped for item in measurements),
            sum(item.lost for item in measurements),
        )

    async def inject_both(self, kind: FaultKind, http: httpx.AsyncClient) -> None:
        await asyncio.gather(*(self.inject(side, kind, http) for side in self.sides.values()))

    async def pause(self) -> None:
        await asyncio.sleep(max(0, self.until - time.monotonic()))

    async def scenario(self, http: httpx.AsyncClient) -> None:
        try:
            await self.pause()
            for index, step in enumerate(STEPS):
                self.current = step
                self.phase = "observe"
                self.step_number = 2 + index * 2
                self.until = time.monotonic() + OBSERVE_SECONDS
                self.observations = {
                    key: Observation(
                        [
                            (client, client.drops, client.lost)
                            for room in side.rooms[1:]
                            for client in room.clients
                        ]
                    )
                    for key, side in self.sides.items()
                }
                for side in self.sides.values():
                    side.running = True
                async with asyncio.TaskGroup() as injections:
                    injections.create_task(self.inject_both(step.kind, http))
                    await self.pause()
                for key, observation in self.observations.items():
                    observation.update(self.sides[key].rooms[1:], self.until)
                self.results.append(
                    Result(
                        step,
                        {
                            key: observation.measurement
                            for key, observation in self.observations.items()
                        },
                        {key: side.result for key, side in self.sides.items()},
                    )
                )
                self.phase = "recovery"
                self.step_number += 1
                self.until = time.monotonic() + RECOVERY_SECONDS
                await self.pause()
            self.phase = "done"
        except asyncio.CancelledError:
            self.phase = "interrupted"
            raise
        finally:
            self.running = False
            for side in self.sides.values():
                side.running = False

    async def inject(self, side: Side, kind: FaultKind, http: httpx.AsyncClient) -> None:
        room = side.rooms[0]
        room.mark(kind, "injecting", time.monotonic())
        side.result = kind + " · injecting into " + room.name
        outcome: Status
        try:
            async with asyncio.timeout(OBSERVE_SECONDS):
                response = await http.post(side.url + "/rooms/" + room.name + "/faults/" + kind)
            detail = "HTTP " + str(response.status_code)
            if response.is_success:
                notice = Fault.model_validate_json(response.content)
                outcome = notice.outcome
            else:
                outcome = "failed"
        except TimeoutError, httpx.TimeoutException:
            outcome, detail = "unknown", "request timed out; outcome unknown"
        except httpx.HTTPError as error:
            outcome, detail = "disconnected", type(error).__name__ + "; request connection lost"
        except ValidationError:
            outcome, detail = "unknown", "invalid fault response"
        room.mark(kind, outcome, time.monotonic())
        side.result = kind + " · " + outcome + " · " + detail
        side.running = False


@dataclass(slots=True)
class Session:
    settings: Settings = field(default_factory=Settings)
    dashboard: Dashboard | None = None


session = Session()
templates = Jinja2Templates(directory=HERE / "crash_templates")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    dashboard = Dashboard(session.settings)
    session.dashboard = dashboard
    async with httpx.AsyncClient(timeout=8, trust_env=False) as http, asyncio.TaskGroup() as group:
        dashboard.group, dashboard.http = group, http
        tasks = [group.create_task(dashboard.sample())]
        for side in dashboard.sides.values():
            for room in side.rooms:
                tasks.extend(
                    group.create_task(dashboard.run_client(side, room, c)) for c in room.clients
                )
        try:
            yield
        finally:
            if dashboard.run_task is not None:
                dashboard.run_task.cancel()
            for task in tasks:
                task.cancel()
            session.dashboard = None


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE.parent / "static"), name="static")
app.mount("/assets", StaticFiles(directory=HERE / "crash_assets"), name="assets")


def dashboard() -> Dashboard:
    if session.dashboard is None:
        raise HTTPException(503, "Dashboard is starting")
    return session.dashboard


type Live = Annotated[Dashboard, Depends(dashboard)]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, live: Live) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html", {"live": live, "now": time.monotonic()}
    )


@app.get("/events", response_class=EventSourceResponse)
async def events(live: Live) -> AsyncIterator[ServerSentEvent]:
    while True:
        yield ServerSentEvent(
            raw_data=templates.get_template("rooms.html").render(
                live=live,
                now=time.monotonic(),
            )
        )
        await asyncio.sleep(0.5)


@app.post(
    "/run",
    response_class=HTMLResponse,
    dependencies=[Depends(require_same_origin)],
)
async def run(request: Request, live: Live) -> HTMLResponse:
    live.start_run()
    return templates.TemplateResponse(
        request, "rooms.html", {"live": live, "now": time.monotonic()}
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Live side-by-side room fault dashboard")
    parser.add_argument("--usual-url", default="http://127.0.0.1:8801")
    parser.add_argument("--pybeam-url", default="http://127.0.0.1:8800")
    parser.add_argument("--rooms", type=int, default=8)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--rate", type=float, default=4, help="Messages per second per socket")
    parser.add_argument("--port", type=int, default=8802)
    session.settings = Settings.model_validate(vars(parser.parse_args()))
    uvicorn.run(app, host="127.0.0.1", port=session.settings.port)


if __name__ == "__main__":
    main()
