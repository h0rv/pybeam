import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html import escape
from importlib.resources import files
from logging import INFO, basicConfig, getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, override

from hornbeam.services.client import Beam
from markupsafe import Markup
from pydantic import BaseModel, Field, ValidationError
from pyview.events import InfoEvent
from pyview.live_socket import ConnectedLiveViewSocket, LiveViewSocket, is_connected
from pyview.live_view import LiveView, Session
from pyview.pyview import PyView
from pyview.template.template_view import TemplateView
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from pybeam.integrations.pyview.subscriptions import ViewSubscription

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from string.templatelib import Template

    from pyview.meta import PyViewMeta
    from pyview.template import RootTemplateContext
    from starlette.applications import Starlette
    from starlette.requests import Request

    from pybeam.integrations.hornbeam import Services

TOPIC = "demo:counter"
logger = getLogger("examples.pyview")
basicConfig(level=INFO, format="%(levelname)s %(name)s: %(message)s")
logger.setLevel(INFO)


@dataclass
class RuntimeState:
    client: Services | None = None

    @property
    def beam(self) -> Services:
        if self.client is None:
            message = "Hornbeam lifespan has not started"
            raise RuntimeError(message)
        return self.client


runtime = RuntimeState()


@asynccontextmanager
async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
    runtime.client = await Beam.connect()
    try:
        yield
    finally:
        runtime.client = None


@dataclass
class CounterState:
    count: int
    amount: int = 1
    error: str = ""


def counter_event(_payload: bytes) -> InfoEvent:
    return InfoEvent(TOPIC)


class CounterView(TemplateView[CounterState], LiveView[CounterState]):
    def __init__(self) -> None:
        super().__init__()
        self.subscription: ViewSubscription | None = None
        self._initialization = asyncio.Lock()

    @override
    async def mount(self, socket: LiveViewSocket[CounterState], session: Session) -> None:
        socket.context = CounterState(0)
        async with self._initialization:
            if is_connected(socket):
                owner = runtime.beam.for_scope(socket.websocket.scope)
                self.subscription = await ViewSubscription.start(
                    owner, socket, TOPIC, counter_event
                )
                logger.info("Subscription connected to %s", TOPIC)
            socket.context.count = await runtime.beam.incr("shared", 0, namespace="demo")

    async def handle_event(
        self, event: str, socket: LiveViewSocket[CounterState], amount: str = ""
    ) -> None:
        if event not in {"increment", "amount"}:
            message = "unexpected counter event"
            raise ValueError(message)
        try:
            data = Increment.model_validate({"amount": amount})
        except ValidationError:
            socket.context.error = "Amount must be an integer from 1 to 10."
            return
        socket.context.amount = data.amount
        socket.context.error = ""
        if event == "increment":
            count = await runtime.beam.incr("shared", data.amount, namespace="demo")
            logger.info("Incremented shared BEAM counter: amount=%s count=%s", data.amount, count)
            await runtime.beam.publish(TOPIC, b"changed")
            logger.info("Published invalidation to %s", TOPIC)

    @override
    async def handle_info(
        self, event: InfoEvent, socket: ConnectedLiveViewSocket[CounterState]
    ) -> None:
        if event.name != TOPIC:
            message = "unexpected counter notification"
            raise ValueError(message)
        async with self._initialization:
            socket.context.count = await runtime.beam.incr("shared", 0, namespace="demo")
            logger.info("Subscription update: fresh BEAM read count=%s", socket.context.count)

    @override
    async def disconnect(self, socket: ConnectedLiveViewSocket[CounterState]) -> None:
        if self.subscription is not None:
            await self.subscription.aclose()
            logger.info("Subscription disconnected from %s", TOPIC)

    @override
    def template(self, assigns: CounterState, meta: PyViewMeta) -> Template:
        return t"""<main>
            <h1>Shared BEAM counter</h1>
            <p>Open two tabs. Increment in one. Both update from BEAM without a reload.</p>
            <p id="counter">{assigns.count}</p>
            <form phx-submit="increment" phx-change="amount">
                <label>Amount <input id="amount" name="amount" type="number" min="1" max="10" value="{assigns.amount}" required></label>
                <button type="submit">Increment</button>
            </form>
            <p role="status">{assigns.error}</p>
            <p>PyView uses its own live renderer. Notifications trigger fresh reads from BEAM ETS.</p>
            <p>Watch the terminal for increments, publications, and subscription activity.</p>
            <p>This public counter is node-local, not persistent, and has no authentication.</p>
        </main>"""


def root_template(context: RootTemplateContext) -> str:
    return default_root(
        {
            **context,
            "id": escape(context["id"], quote=True),
            "csrf_token": escape(context["csrf_token"], quote=True),
            "session": escape(context["session"] or "", quote=True),
            "title": escape(context["title"] or "Shared BEAM counter"),
            "additional_head_elements": [
                *context["additional_head_elements"],
                Markup('<link rel="stylesheet" href="/demo-static/missing.min.css">'),
                Markup('<link rel="icon" href="data:,">'),
            ],
        }
    )


class Increment(BaseModel):
    amount: Annotated[int, Field(ge=1, le=10)]


async def health(_request: Request) -> Response:
    await runtime.beam.incr("shared", 0, namespace="demo")
    return PlainTextResponse("ok")


async def counter(_request: Request) -> Response:
    value = await runtime.beam.incr("shared", 0, namespace="demo")
    return PlainTextResponse(str(value))


app = PyView(
    lifespan=lifespan,
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/counter", counter, methods=["GET"]),
        Mount("/static", StaticFiles(directory=str(files("pyview").joinpath("static")))),
        Mount("/demo-static", StaticFiles(directory=Path(__file__).parent.parent / "static")),
    ],
)
default_root = app.rootTemplate
app.rootTemplate = root_template
app.add_live_view("/", CounterView)
