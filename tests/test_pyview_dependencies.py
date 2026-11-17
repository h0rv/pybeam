import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from hornbeam.api.errors import RuntimeProtocolError
from hornbeam.services.client import Beam as NativeBeam
from pyview import Depends
from pyview.binding.binder import Binder
from pyview.binding.context import BindContext
from pyview.binding.params import Params
from pyview.events import InfoEvent
from pyview.instrumentation import NoOpInstrumentation
from pyview.live_socket import ConnectedLiveViewSocket, UnconnectedSocket
from pyview.live_view import LiveView
from pyview.pyview import PyView
from starlette.types import Message, Scope
from starlette.websockets import WebSocket

from pybeam.integrations.hornbeam import Beam, Services
from pybeam.integrations.pyview.dependencies import BeamProvider, services
from tests.fakes import FakeServices, ServiceState


async def test_binder_injects_socket_into_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    beam = FakeServices(ServiceState(), owner="app")
    calls = 0

    async def connect() -> Services:
        nonlocal calls
        calls += 1
        return beam

    monkeypatch.setattr(services, "_connect", connect)
    socket = UnconnectedSocket[int]()

    async def handler(beam: Services = Depends(services)) -> None:
        assert isinstance(beam, FakeServices)

    app = PyView(lifespan=services.lifespan)
    async with app.router.lifespan_context(app):
        context = BindContext(Params({}), None, None, socket, None)
        result = await Binder[int]().abind(handler, context)
        assert result.success, result.errors
        assert result.bound_args["beam"] is beam
        await handler(services(socket))
        with pytest.raises(RuntimeError, match="already running"):
            async with services.lifespan(app):
                pytest.fail("nested lifespan accepted")
    assert calls == 1
    with pytest.raises(RuntimeError, match="active application lifespan"):
        services(socket)


async def test_failed_startup_can_be_retried() -> None:
    attempts = 0
    beam = FakeServices(ServiceState(), owner="app")

    async def connect() -> Services:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("connection failed")
        return beam

    instance = BeamProvider(connect)
    with pytest.raises(ValueError, match="connection failed"):
        async with instance.lifespan(object()):
            pytest.fail("startup accepted")
    with pytest.raises(RuntimeError, match="active application lifespan"):
        instance(UnconnectedSocket[int]())
    with pytest.raises(ValueError, match="application failed"):
        async with instance.lifespan(object()):
            assert instance.services is beam
            message = "application failed"
            raise ValueError(message)
    with pytest.raises(RuntimeError, match="active application lifespan"):
        _ = instance.services


def connected_socket(scope: Scope) -> ConnectedLiveViewSocket[int]:
    async def receive() -> Message:
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: Message) -> None:
        pytest.fail("unexpected websocket output: " + repr(message))

    return ConnectedLiveViewSocket[int](
        WebSocket(scope, receive, send),
        "lv:test",
        LiveView[int](),
        AsyncIOScheduler(),
        NoOpInstrumentation(),
    )


def notification(payload: bytes) -> InfoEvent:
    return InfoEvent("topic", payload)


async def test_subscription_uses_socket_owner_and_closes() -> None:
    state = ServiceState()
    beam = FakeServices(state, owner="application")

    async def connect() -> Services:
        return beam

    instance = BeamProvider(connect)
    socket = connected_socket(
        {"type": "websocket", "extensions": {"hornbeam.owner": "socket-owner"}}
    )
    async with instance.lifespan(object()):
        assert instance(socket) is beam
        subscription = await instance.subscribe(socket, "topic", notification, max_bytes=1024)
        try:
            assert state.calls == [("subscribe", ["socket-owner", "topic", 1024])]
            assert len(state.channels) == 1
        finally:
            await subscription.aclose()
        assert not state.channels
    with pytest.raises(RuntimeError, match="active application lifespan"):
        await instance.subscribe(socket, "topic", notification)


@pytest.mark.parametrize("extensions", [None, {}, {"hornbeam.owner": None}])
async def test_subscription_requires_native_scope_owner(
    extensions: dict[str, object] | None,
) -> None:
    beam = Beam(NativeBeam(owner="application"))

    async def connect() -> Services:
        return beam

    instance = BeamProvider(connect)
    scope: Scope = {"type": "websocket"}
    if extensions is not None:
        scope["extensions"] = extensions
    socket = connected_socket(scope)
    async with instance.lifespan(object()):
        assert instance(socket) is beam
        with pytest.raises(RuntimeProtocolError, match="scope has no"):
            await instance.subscribe(socket, "topic", notification)


@pytest.mark.parametrize(("topic", "max_bytes"), [("", 1024), ("topic", 1)])
async def test_subscription_preserves_native_validation(topic: str, max_bytes: int) -> None:
    beam = Beam(NativeBeam(owner="application"))

    async def connect() -> Services:
        return beam

    instance = BeamProvider(connect)
    socket = connected_socket(
        {"type": "websocket", "extensions": {"hornbeam.owner": "socket-owner"}}
    )
    async with instance.lifespan(object()):
        with pytest.raises(ValueError):
            await instance.subscribe(socket, topic, notification, max_bytes=max_bytes)
