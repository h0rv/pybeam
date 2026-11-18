from dataclasses import dataclass
from secrets import token_urlsafe
from string.templatelib import Template
from typing import override

from pyview import Depends
from pyview.auth import requires
from pyview.events import InfoEvent
from pyview.live_socket import ConnectedLiveViewSocket, LiveViewSocket, is_connected
from pyview.live_view import LiveView, Session
from pyview.meta import PyViewMeta
from pyview.pyview import PyView
from pyview.template.template_view import TemplateView
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    BaseUser,
    SimpleUser,
)
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from pybeam.integrations.hornbeam import Services
from pybeam.integrations.pyview.dependencies import services
from pybeam.integrations.pyview.subscriptions import ViewSubscription


class Authentication(AuthenticationBackend):
    @override
    async def authenticate(self, conn: HTTPConnection) -> tuple[AuthCredentials, BaseUser] | None:
        if conn.scope["type"] == "websocket":
            await services.services.incr("auth", namespace="pyview-reconnect")
        if conn.session.get("authorized") is True:
            return AuthCredentials(["authenticated"]), SimpleUser("reader")
        return None


@dataclass
class State:
    count: int
    notices: int = 0


@requires("authenticated")
class CounterView(TemplateView[State], LiveView[State]):
    subscription: ViewSubscription | None = None

    @override
    async def mount(
        self,
        socket: LiveViewSocket[State],
        session: Session,
        beam: Services = Depends(services),
    ) -> None:
        if is_connected(socket):
            await beam.incr("mounts", namespace="pyview-reconnect")
            self.subscription = await services.subscribe(
                socket, "pyview-reconnect:changed", lambda value: InfoEvent("changed", value)
            )
        socket.context = State(await beam.incr("value", 0, namespace="pyview-reconnect"))

    @override
    async def handle_info(self, event: InfoEvent, socket: ConnectedLiveViewSocket[State]) -> None:
        socket.context.count = await services.services.incr(
            "value", 0, namespace="pyview-reconnect"
        )
        socket.context.notices += 1

    @override
    async def disconnect(self, socket: ConnectedLiveViewSocket[State]) -> None:
        if self.subscription is not None:
            await self.subscription.aclose()

    @override
    def template(self, assigns: State, meta: PyViewMeta) -> Template:
        return t"<p>{assigns.count}</p><span>{assigns.notices}</span>"


async def session(request: Request) -> JSONResponse:
    request.session["authorized"] = request.query_params.get("token") == "valid"
    return JSONResponse({"ok": True})


async def health(request: Request) -> JSONResponse:
    beam = services.services
    return JSONResponse(
        {
            "auth": await beam.incr("auth", 0, namespace="pyview-reconnect"),
            "mounts": await beam.incr("mounts", 0, namespace="pyview-reconnect"),
            "count": await beam.subscription_count(),
        }
    )


async def increment(request: Request) -> JSONResponse:
    beam = services.services
    count = await beam.incr("value", namespace="pyview-reconnect")
    targeted = await beam.publish("pyview-reconnect:changed", b"invalidate")
    return JSONResponse({"count": count, "targeted": targeted})


app = PyView(lifespan=services.lifespan)
app.add_middleware(AuthenticationMiddleware, backend=Authentication())
app.add_middleware(SessionMiddleware, secret_key=token_urlsafe())
app.routes.extend(
    [
        Route("/session", session),
        Route("/health", health),
        Route("/increment", increment, methods=["POST"]),
    ]
)
app.add_live_view("/", CounterView)
