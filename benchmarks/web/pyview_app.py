from dataclasses import dataclass
from string.templatelib import Template
from typing import override

from pyview.events import InfoEvent
from pyview.live_socket import ConnectedLiveViewSocket, LiveViewSocket, is_connected
from pyview.live_view import LiveView, Session
from pyview.meta import PyViewMeta
from pyview.pyview import PyView
from pyview.template.template_view import TemplateView
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route


@dataclass
class State:
    ticks: int = 0
    events: int = 0


class View(TemplateView[State], LiveView[State]):
    @override
    async def mount(self, socket: LiveViewSocket[State], session: Session) -> None:
        socket.context = State()
        if is_connected(socket):
            socket.schedule_info(InfoEvent("tick"), 1)

    @override
    async def handle_info(self, event: InfoEvent, socket: ConnectedLiveViewSocket[State]) -> None:
        socket.context.ticks += 1

    async def handle_event(self, event: str, socket: LiveViewSocket[State]) -> None:
        if event != "probe":
            raise ValueError("Unexpected benchmark event")
        socket.context.events += 1

    @override
    def template(self, assigns: State, meta: PyViewMeta) -> Template:
        return t'<p id="ticks">{assigns.ticks}</p><p>{assigns.events}</p>'


async def ping(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


app = PyView(routes=[Route("/ping", ping)])
app.add_live_view("/", View)
