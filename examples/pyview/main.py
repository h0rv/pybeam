import asyncio
from dataclasses import dataclass
from html import escape
from importlib.resources import files
from logging import INFO, basicConfig, getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, override

from markupsafe import Markup
from pydantic import BaseModel, Field, ValidationError
from pyview import Depends
from pyview.events import InfoEvent
from pyview.live_socket import ConnectedLiveViewSocket, LiveViewSocket, is_connected
from pyview.live_view import LiveView, Session
from pyview.pyview import PyView
from pyview.template.template_view import TemplateView
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from pybeam.integrations.hornbeam import Services
from pybeam.integrations.pyview.dependencies import services
from pybeam.integrations.pyview.subscriptions import ViewSubscription

if TYPE_CHECKING:
    from string.templatelib import Template

    from pyview.meta import PyViewMeta
    from pyview.template import RootTemplateContext
    from starlette.requests import Request


TOPIC = "demo:counter"
logger = getLogger("examples.pyview")
basicConfig(level=INFO, format="%(levelname)s %(name)s: %(message)s")
logger.setLevel(INFO)


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
        self._beam: Services | None = None
        self._initialization = asyncio.Lock()

    @override
    async def mount(
        self,
        socket: LiveViewSocket[CounterState],
        session: Session,
        beam: Services = Depends(services),
    ) -> None:
        socket.context = CounterState(0)
        self._beam = beam
        async with self._initialization:
            if is_connected(socket):
                self.subscription = await services.subscribe(socket, TOPIC, counter_event)
                logger.info("Subscription connected to %s", TOPIC)
            socket.context.count = await beam.incr("shared", 0, namespace="demo")

    async def handle_event(
        self,
        event: str,
        socket: LiveViewSocket[CounterState],
        amount: str = "",
        beam: Services = Depends(services),
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
            count = await beam.incr("shared", data.amount, namespace="demo")
            logger.info("Incremented shared BEAM counter: amount=%s count=%s", data.amount, count)
            await beam.publish(TOPIC, b"changed")
            logger.info("Published invalidation to %s", TOPIC)

    @override
    async def handle_info(
        self,
        event: InfoEvent,
        socket: ConnectedLiveViewSocket[CounterState],
    ) -> None:
        if event.name != TOPIC:
            message = "unexpected counter notification"
            raise ValueError(message)
        async with self._initialization:
            if self._beam is None:
                message = "CounterView has not mounted"
                raise RuntimeError(message)
            socket.context.count = await self._beam.incr("shared", 0, namespace="demo")
            logger.info("Subscription update: fresh BEAM read count=%s", socket.context.count)

    @override
    async def disconnect(self, socket: ConnectedLiveViewSocket[CounterState]) -> None:
        if self.subscription is not None:
            await self.subscription.aclose()
            logger.info("Subscription disconnected from %s", TOPIC)

    @override
    def template(self, assigns: CounterState, meta: PyViewMeta) -> Template:
        return t"""<main>
            <h1>Shared PyBEAM counter</h1>
            <p>Open two tabs. Increment in one. Both update without a reload.</p>
            <p id="counter">{assigns.count}</p>
            <form phx-submit="increment" phx-change="amount">
                <label>Amount <input id="amount" name="amount" type="number" min="1" max="10" value="{assigns.amount}" required></label>
                <button type="submit">Increment</button>
            </form>
            <p role="status">{assigns.error}</p>
            <p>PyView uses its own live renderer. Notifications trigger fresh counter reads.</p>
            <p>Watch the terminal for increments, publications, and subscription activity.</p>
            <p>Everyone connected to this app shares the counter. Restarting the app clears it. This demo has no authentication.</p>
        </main>"""


def root_template(context: RootTemplateContext) -> str:
    return default_root(
        {
            **context,
            "id": escape(context["id"], quote=True),
            "csrf_token": escape(context["csrf_token"], quote=True),
            "session": escape(context["session"] or "", quote=True),
            "title": escape(context["title"] or "Shared PyBEAM counter"),
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
    await services.services.incr("shared", 0, namespace="demo")
    return PlainTextResponse("ok")


async def counter(_request: Request) -> Response:
    value = await services.services.incr("shared", 0, namespace="demo")
    return PlainTextResponse(str(value))


app = PyView(
    lifespan=services.lifespan,
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
