import argparse
import asyncio
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

    async def receive(self, socket: ClientConnection, room: Room, client: Client) -> None:
        while True:
            push = PUSH.validate_json(await socket.recv())
            now = time.monotonic()
            match push:
                case Ack(id=sequence):
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
                    if connected_before:
                        client.reconnects += 1
                    connected_before = True
                    client.state = "connected"
                    client.error = ""
                    async with asyncio.TaskGroup() as tasks:
                        tasks.create_task(self.send(socket, client))
                        tasks.create_task(self.receive(socket, room, client))
            except* (
                OSError,
                ConnectionClosed,
                InvalidHandshake,
                TimeoutError,
                ValidationError,
            ) as errors:
                client.error = str(errors.exceptions[0])
            finally:
                client.state = "disconnected"
                client.lost += len(client.pending)
                client.pending.clear()
            await asyncio.sleep(0.75)
            client.state = "reconnecting"
            await asyncio.sleep(0.75)

    async def sample(self) -> None:
        while True:
            now = time.monotonic()
            for side in self.sides.values():
                for room in side.rooms:
                    room.sample(now)
            await asyncio.sleep(0.5)

    async def inject(self, side: Side, kind: FaultKind, http: httpx.AsyncClient) -> None:
        room = side.rooms[0]
        room.mark(kind, "injecting", time.monotonic())
        side.result = kind + " · injecting into " + room.name
        outcome: Status
        try:
            response = await http.post(side.url + "/rooms/" + room.name + "/faults/" + kind)
            detail = "HTTP " + str(response.status_code)
            if response.is_success:
                notice = Fault.model_validate_json(response.content)
                outcome = notice.outcome
            else:
                outcome = "failed"
        except httpx.TimeoutException:
            outcome, detail = "unknown", "request timed out; outcome unknown"
        except httpx.HTTPError as error:
            outcome, detail = "disconnected", type(error).__name__ + "; request connection lost"
        except ValidationError:
            outcome, detail = "unknown", "invalid fault response"
        room.mark(kind, outcome, time.monotonic())
        side.result = kind + " · " + outcome + " · " + detail


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
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(dashboard.sample())]
        for side in dashboard.sides.values():
            for room in side.rooms:
                tasks.extend(
                    group.create_task(dashboard.run_client(side, room, c)) for c in room.clients
                )
        try:
            yield
        finally:
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


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    expected = request.url.scheme + "://" + request.url.netloc
    if request.headers.get("sec-fetch-site") not in (None, "none", "same-origin") or (
        origin is not None and origin != expected
    ):
        raise HTTPException(403, "Cross-origin mutations are not allowed")


async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=12, trust_env=False) as http:
        yield http


type Http = Annotated[httpx.AsyncClient, Depends(http_client)]


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
    "/faults/{side_id}/{kind}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_same_origin)],
)
async def inject(
    request: Request, side_id: SideId, kind: FaultKind, live: Live, http: Http
) -> HTMLResponse:
    side = live.sides[side_id]
    if not side.running:
        side.running = True
        try:
            await live.inject(side, kind, http)
        finally:
            side.running = False
    return templates.TemplateResponse(request, "result.html", {"side": side})


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
