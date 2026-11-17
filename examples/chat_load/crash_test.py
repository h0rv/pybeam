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
    "waiting",
]

HERE = Path(__file__).parent
PUSH: TypeAdapter[Push] = TypeAdapter(Push)
BASELINE_SECONDS = 4.0
OBSERVE_SECONDS = 10.0
RECOVERY_SECONDS = 5.0
DEFAULT_RATE = 1.0
CHAT = (
    "Anyone here?",
    "Can you still see my messages?",
    "Keeping this conversation going.",
    "Another message from this room.",
    "Still here. How about you?",
    "Let's keep chatting.",
)


@dataclass(frozen=True, slots=True)
class Step:
    kind: FaultKind
    title: str
    simulates: str


STEPS = (
    Step(
        "raise",
        "A handler raises",
        "A bug in one room's handler raises an exception.",
    ),
    Step(
        "hog",
        "A room hogs the CPU",
        "One room runs 3 seconds of CPU-bound work.",
    ),
    Step(
        "abort",
        "A native process crashes",
        "Native code in one room crashes its process: os.abort(), like a segfault in a C extension.",
    ),
)


@dataclass(slots=True)
class Measurement:
    p99: float | None = None
    pending_ms: float = 0
    dropped: int = 0
    lost: int = 0
    replies: int = 0


@dataclass(slots=True)
class Observation:
    clients: list[tuple[Client, int, int]]
    measurement: Measurement = field(default_factory=Measurement)
    replies_before: int = 0

    def update(self, rooms: list[Room], now: float) -> None:
        for room in rooms:
            if room.p99 is not None:
                previous = self.measurement.p99
                self.measurement.p99 = max(previous or 0, room.p99)
            self.measurement.pending_ms = max(self.measurement.pending_ms, room.oldest_ms(now))
        self.measurement.dropped = sum(client.drops > before for client, before, _ in self.clients)
        self.measurement.lost = sum(client.lost - before for client, _, before in self.clients)
        self.measurement.replies = (
            sum(client.replies for client, _, _ in self.clients) - self.replies_before
        )


@dataclass(frozen=True, slots=True)
class Result:
    step: Step
    measurements: dict[SideId, Measurement]
    outcomes: dict[SideId, str]


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    usual_url: HttpUrl = HttpUrl("http://127.0.0.1:8801")
    pybeam_url: HttpUrl = HttpUrl("http://127.0.0.1:8800")
    rooms: int = Field(default=8, ge=2, le=32)
    clients: int = Field(default=4, ge=1, le=16)
    rate: float = Field(default=DEFAULT_RATE, ge=0.1, le=20)
    port: int = Field(default=8802, ge=1024, le=65535)


@dataclass(slots=True)
class ChatMessage:
    sequence: int
    text: str
    sent: float
    state: Literal["pending", "confirmed", "unconfirmed"] = "pending"

    def age(self, now: float) -> str:
        return "{:.1f} s".format(max(0, now - self.sent))


@dataclass(slots=True)
class Client:
    name: str
    state: Literal["connected", "disconnected", "reconnecting"] = "reconnecting"
    pending: dict[int, float] = field(default_factory=dict)
    reconnects: int = 0
    lost: int = 0
    drops: int = 0
    error: str = ""
    messages: deque[ChatMessage] = field(default_factory=lambda: deque(maxlen=3))
    sequence: int = 0
    replies: int = 0
    last_reply: float = 0


@dataclass(slots=True)
class Room:
    name: str
    clients: list[Client]
    samples: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=4096))
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

    @property
    def connected(self) -> int:
        return sum(client.state == "connected" for client in self.clients)

    def status(self, now: float) -> Status:
        if any(client.state == "disconnected" for client in self.clients):
            return "disconnected"
        if self.connected < len(self.clients):
            return "reconnecting"
        if self.oldest_ms(now) >= 500:
            return "waiting"
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
        self.focus_room = 1
        self.focus_client = 0
        self.focus_reason = "The same chat user on both sides."
        self.focus_dropped = False
        self.focus_delayed = False
        tag = uuid4().hex[:8]
        self.sides: dict[SideId, Side] = {}
        targets: tuple[tuple[SideId, str, HttpUrl], ...] = (
            ("usual", "Usual stack", settings.usual_url),
            ("pybeam", "PyBEAM", settings.pybeam_url),
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
        while True:
            now = time.monotonic()
            if len(client.pending) >= 64 or any(now - sent > 8 for sent in client.pending.values()):
                raise TimeoutError("Acknowledgements stalled")
            sequence = client.sequence
            message = ChatMessage(sequence, CHAT[sequence % len(CHAT)], now)
            client.messages.append(message)
            client.pending[sequence] = now
            await socket.send(Send(id=sequence, text=message.text).model_dump_json())
            client.sequence += 1
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
                        client.replies += 1
                        client.last_reply = now
                        for message in client.messages:
                            if message.sequence == sequence:
                                message.state = "confirmed"
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
                for message in client.messages:
                    if message.state == "pending":
                        message.state = "unconfirmed"
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
            self.select_focus(now)

    def select_focus(self, now: float) -> None:
        if self.focus_dropped:
            return
        for key, observation in self.observations.items():
            side = self.sides[key]
            for client, drops, _ in observation.clients:
                if client.drops > drops:
                    self.focus_room = next(
                        i for i, room in enumerate(side.rooms) if client in room.clients
                    )
                    self.focus_client = side.rooms[self.focus_room].clients.index(client)
                    self.focus_reason = (
                        "This user disconnected on "
                        + side.title
                        + "; showing the matching user on both sides."
                    )
                    self.focus_dropped = True
                    return
        if self.focus_delayed:
            return
        candidates = [
            (now - started, room_index, client_index)
            for side in self.sides.values()
            for room_index, room in enumerate(side.rooms[1:], 1)
            for client_index, client in enumerate(room.clients)
            for started in client.pending.values()
        ]
        age, room_index, client_index = max(candidates, default=(0, 1, 0))
        if age >= 0.5:
            self.focus_room, self.focus_client = room_index, client_index
            self.focus_reason = (
                "A reply is delayed for this user; showing the matching user on both sides."
            )
            self.focus_delayed = True

    @property
    def ready(self) -> bool:
        now = time.monotonic()
        return all(
            client.state == "connected"
            and client.replies > 0
            and now - client.last_reply < max(3, 2 / self.settings.rate)
            for side in self.sides.values()
            for room in side.rooms
            for client in room.clients
        )

    def impact(self, key: SideId) -> Measurement | None:
        if key in self.observations:
            return self.observations[key].measurement
        return None

    def focus_interrupted(self, key: SideId) -> bool:
        observation = self.observations.get(key)
        client = self.sides[key].rooms[self.focus_room].clients[self.focus_client]
        return observation is not None and any(
            item is client and item.drops > before for item, before, _ in observation.clients
        )

    async def sample(self) -> None:
        while True:
            self.sample_once(time.monotonic())
            await asyncio.sleep(0.5)

    def remaining(self, now: float) -> int:
        return math.ceil(max(0, self.until - now)) if self.running else 0

    def start_run(self, kind: FaultKind = "abort") -> None:
        if self.running:
            raise HTTPException(409, "A crash test is already running")
        if self.group is None or self.http is None:
            raise HTTPException(503, "Scenario runner is starting")
        if not self.ready:
            raise HTTPException(503, "Waiting for live replies from every chat user on both stacks")
        self.running = True
        self.phase = "baseline"
        self.step_number = 1
        self.until = time.monotonic() + BASELINE_SECONDS
        self.current = next(step for step in STEPS if step.kind == kind)
        self.focus_room, self.focus_client = 1, 0
        self.focus_reason = "The same chat user on both sides."
        self.focus_dropped = False
        self.focus_delayed = False
        self.observations.clear()
        self.results.clear()
        self.run_task = self.group.create_task(self.scenario(self.http, self.current))

    async def inject_both(self, kind: FaultKind, http: httpx.AsyncClient) -> None:
        async with asyncio.TaskGroup() as tasks:
            for side in self.sides.values():
                tasks.create_task(self.inject(side, kind, http))

    async def pause(self) -> None:
        await asyncio.sleep(max(0, self.until - time.monotonic()))

    async def scenario(self, http: httpx.AsyncClient, step: Step) -> None:
        try:
            await self.pause()
            if not self.ready:
                self.phase = "interrupted"
                return
            self.phase = "observe"
            self.step_number = 2
            self.until = time.monotonic() + OBSERVE_SECONDS
            self.observations = {
                key: Observation(
                    [
                        (client, client.drops, client.lost)
                        for room in side.rooms[1:]
                        for client in room.clients
                    ],
                    replies_before=sum(
                        client.replies for room in side.rooms[1:] for client in room.clients
                    ),
                )
                for key, side in self.sides.items()
            }
            for side in self.sides.values():
                side.running = True
                for room in side.rooms:
                    room.samples.clear()
                    room.p99 = None
                    room.sample_count = 0
            async with asyncio.TaskGroup() as injections:
                injections.create_task(self.inject_both(step.kind, http))
                await self.pause()
            self.sample_once(self.until)
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
            self.step_number = 3
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


@app.post(
    "/run/{kind}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_same_origin)],
)
async def run_fault(request: Request, live: Live, kind: FaultKind) -> HTMLResponse:
    live.start_run(kind)
    return templates.TemplateResponse(
        request, "rooms.html", {"live": live, "now": time.monotonic()}
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Live side-by-side room fault dashboard")
    parser.add_argument("--usual-url", default="http://127.0.0.1:8801")
    parser.add_argument("--pybeam-url", default="http://127.0.0.1:8800")
    parser.add_argument("--rooms", type=int, default=8)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument(
        "--rate", type=float, default=DEFAULT_RATE, help="Messages per second per socket"
    )
    parser.add_argument("--port", type=int, default=8802)
    session.settings = Settings.model_validate(vars(parser.parse_args()))
    uvicorn.run(app, host="127.0.0.1", port=session.settings.port)


if __name__ == "__main__":
    main()
