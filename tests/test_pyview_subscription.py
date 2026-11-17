import asyncio
from re import escape
from typing import TYPE_CHECKING

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from hornbeam.api.errors import RuntimeUnavailable
from httpx import ASGITransport, AsyncClient
from pyview import Depends
from pyview.binding.binder import Binder
from pyview.binding.context import BindContext
from pyview.binding.params import Params
from pyview.events import InfoEvent
from pyview.instrumentation import NoOpInstrumentation
from pyview.live_socket import ConnectedLiveViewSocket, UnconnectedSocket
from starlette.websockets import WebSocket

from examples.pyview.main import (
    CounterState,
    CounterView,
    app,
    counter_event,
    root_template,
)
from pybeam.integrations.hornbeam import Services
from pybeam.integrations.hornbeam.errors import SubscriptionOverflow
from pybeam.integrations.pyview.dependencies import services
from pybeam.integrations.pyview.subscriptions import ViewSubscription
from tests.fakes import FakeServices as Beam
from tests.fakes import ServiceState


class TestDoubleBridge(ServiceState):
    __test__ = False

    def __init__(self) -> None:
        super().__init__(capacity=2)


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from starlette.types import Message


class TestDoubleSocket:
    __test__ = False

    def __init__(self, *, fail: bool = False) -> None:
        self.events: asyncio.Queue[InfoEvent] = asyncio.Queue(maxsize=8)
        self.fail = fail
        self.active = 0
        self.max_active = 0

    async def send_info(self, event: InfoEvent) -> None:
        if self.fail:
            message = "render failed"
            raise ValueError(message)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            self.events.put_nowait(event)
        finally:
            self.active -= 1

    async def received(self) -> InfoEvent:
        async with asyncio.timeout(1):
            return await self.events.get()


def notification(payload: bytes) -> InfoEvent:
    return InfoEvent("demo:counter", int(payload.decode("ascii")))


async def test_two_views__should_keep_request_owners_and_cleanup_independent() -> None:
    bridge = TestDoubleBridge()
    app = Beam(bridge, owner="fallback")
    first = app.for_scope({"extensions": {"hornbeam.owner": "websocket-1"}})
    second = app.for_scope({"extensions": {"hornbeam.owner": "websocket-2"}})
    socket1, socket2 = TestDoubleSocket(), TestDoubleSocket()
    view1 = await ViewSubscription.start(first, socket1, "demo:counter", notification)
    view2 = await ViewSubscription.start(second, socket2, "demo:counter", notification)
    try:
        owners = [args[0] for action, args in bridge.calls if action == "subscribe"]
        assert owners == ["websocket-1", "websocket-2"]
        assert await app.publish("demo:counter", b"4") == 2
        assert (await socket1.received()).payload == 4
        assert (await socket2.received()).payload == 4
        await view1.aclose()
        assert await app.subscription_count() == 1
        assert await app.publish("demo:counter", b"5") == 1
        assert (await socket2.received()).payload == 5
        assert socket1.events.empty()
    finally:
        await view1.aclose()
        await view2.aclose()
    assert await app.subscription_count() == 0
    assert socket2.max_active == 1
    closed_owners = [args[0] for action, args in bridge.calls if action == "unsubscribe"]
    assert closed_owners == ["websocket-1", "websocket-2"]


async def test_wait__cancelled_observer__should_leave_owned_consumer_running() -> None:
    bridge = TestDoubleBridge()
    beam = Beam(bridge, owner="websocket")
    socket = TestDoubleSocket()
    view = await ViewSubscription.start(beam, socket, "demo:counter", notification)
    try:
        observer = asyncio.create_task(view.wait())
        await asyncio.sleep(0)
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert await beam.publish("demo:counter", b"6") == 1
        assert (await socket.received()).payload == 6
    finally:
        await view.aclose()


async def test_lifespan__outside_hornbeam__should_fail_without_memory_fallback() -> None:
    with pytest.raises(RuntimeUnavailable):
        async with services.lifespan(app):
            pytest.fail("startup unexpectedly succeeded outside Hornbeam")
    with pytest.raises(RuntimeError, match="active application lifespan"):
        _ = services.services


async def test_close__before_first_task_step__should_release_subscription() -> None:
    bridge = TestDoubleBridge()
    beam = Beam(bridge, owner="websocket")
    view = await ViewSubscription.start(beam, TestDoubleSocket(), "demo:counter", notification)
    await view.aclose()
    await view.aclose()
    assert await beam.subscription_count() == 0


async def test_close__pending_receive__should_cancel_and_await_cleanup() -> None:
    bridge = TestDoubleBridge()
    beam = Beam(bridge, owner="websocket")
    view = await ViewSubscription.start(beam, TestDoubleSocket(), "demo:counter", notification)
    channel = next(iter(bridge.channels.values()))
    async with asyncio.timeout(1):
        await channel.entered.wait()
    await view.aclose()
    assert await beam.subscription_count() == 0


async def test_close__caller_cancelled__should_finish_cleanup_then_preserve_cancellation() -> None:
    bridge = TestDoubleBridge()
    bridge.close_gate.clear()
    beam = Beam(bridge, owner="websocket")
    view = await ViewSubscription.start(beam, TestDoubleSocket(), "demo:counter", notification)
    closing = asyncio.create_task(view.aclose())
    async with asyncio.timeout(1):
        await bridge.close_started.wait()
    closing.cancel()
    bridge.close_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await view.aclose()
    assert await beam.subscription_count() == 0


async def test_converter_error__should_be_logged_rethrown_and_close_subscription(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = TestDoubleBridge()
    beam = Beam(bridge, owner="websocket")
    view = await ViewSubscription.start(beam, TestDoubleSocket(), "demo:counter", notification)
    await beam.publish("demo:counter", b"not-a-number")
    with pytest.raises(ValueError, match="invalid literal"):
        await view.wait()
    with pytest.raises(ValueError, match="invalid literal"):
        await view.aclose()
    assert "PyView BEAM subscription failed" in caplog.text
    assert await beam.subscription_count() == 0


async def test_overflow__should_not_be_silently_treated_as_disconnect() -> None:
    bridge = TestDoubleBridge()
    beam = Beam(bridge, owner="websocket")
    view = await ViewSubscription.start(beam, TestDoubleSocket(), "demo:counter", notification)
    for value in [b"1", b"2", b"3"]:
        await beam.publish("demo:counter", value)
    with pytest.raises(SubscriptionOverflow):
        await view.wait()
    with pytest.raises(SubscriptionOverflow):
        await view.aclose()
    assert await beam.subscription_count() == 0


async def test_render_error__should_propagate_and_close_subscription() -> None:
    bridge = TestDoubleBridge()
    beam = Beam(bridge, owner="websocket")
    view = await ViewSubscription.start(
        beam, TestDoubleSocket(fail=True), "demo:counter", notification
    )
    await beam.publish("demo:counter", b"1")
    with pytest.raises(ValueError, match="render failed"):
        await view.wait()
    with pytest.raises(ValueError, match="render failed"):
        await view.aclose()
    assert await beam.subscription_count() == 0


@pytest.mark.parametrize("failure_kind", ["render", "converter"])
async def test_close__failure_during_cleanup__should_preserve_original_and_log_before_ack(
    caplog: pytest.LogCaptureFixture, failure_kind: str
) -> None:
    bridge = TestDoubleBridge()
    bridge.close_gate.clear()
    beam = Beam(bridge, owner="websocket")
    view = await ViewSubscription.start(
        beam, TestDoubleSocket(fail=failure_kind == "render"), "demo:counter", notification
    )
    closing: asyncio.Task[None] | None = None
    try:
        await beam.publish("demo:counter", b"1" if failure_kind == "render" else b"invalid")
        async with asyncio.timeout(1):
            await bridge.close_started.wait()
        records = [record for record in caplog.records if record.exc_info is not None]
        assert len(records) == 1
        exc_info = records[0].exc_info
        assert isinstance(exc_info, tuple)
        original = exc_info[1]
        assert isinstance(original, ValueError)
        closing = asyncio.create_task(view.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        bridge.close_gate.set()
    assert closing is not None
    with pytest.raises(ValueError, match=escape(str(original))) as closed:
        await closing
    assert closed.value is original
    with pytest.raises(ValueError, match=escape(str(original))) as observed:
        await view.wait()
    assert observed.value is original
    with pytest.raises(ValueError, match=escape(str(original))) as repeated:
        await view.aclose()
    assert repeated.value is original
    assert await beam.subscription_count() == 0
    assert sum(action == "unsubscribe" for action, _ in bridge.calls) == 1
    assert caplog.text.count("PyView BEAM subscription failed") == 1


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[AsyncClient]:
    bridge = TestDoubleBridge()

    async def connect() -> Beam:
        return Beam(bridge, owner="explicit-test-double")

    monkeypatch.setattr(services, "_connect", connect)
    async with (
        services.lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://demo") as client,
    ):
        yield client


async def test_http_demo__should_serve_readiness_root_and_packaged_client(
    client: AsyncClient,
) -> None:
    assert (await client.get("/health")).text == "ok"
    assert (await client.get("/counter")).text == "0"
    page = await client.get("/")
    assert page.status_code == 200
    assert 'id="counter"' in page.text
    assert 'phx-submit="increment"' in page.text
    assert 'phx-change="amount"' in page.text
    assert 'action="/increment"' not in page.text
    assert page.text.index('id="counter"') < page.text.index("<form")
    assert 'href="/demo-static/missing.min.css"' in page.text
    assert 'rel="icon" href="data:,"' in page.text
    assert (await client.get("/static/assets/app.js")).status_code == 200
    assert await services.services.subscription_count() == 0


async def test_live_form__should_retain_amount_and_publish_invalidation(
    client: AsyncClient,
) -> None:
    view = CounterView()
    socket = UnconnectedSocket[CounterState]()
    await view.mount(socket, {}, beam=services(socket))
    async with services.services.subscribe("demo:counter") as subscription:
        await view.handle_event("amount", socket, "10", beam=services(socket))
        assert (await client.get("/counter")).text == "0"
        await view.handle_event("increment", socket, "10", beam=services(socket))
        assert await subscription.receive() == b"changed"
    assert socket.context.amount == 10
    assert socket.context.error == ""
    assert (await client.get("/counter")).text == "10"


@pytest.mark.parametrize("amount", ["", "0", "11", "-1", "1.5", "nan", "<script>"])
async def test_live_form__invalid_amount__should_reject(client: AsyncClient, amount: str) -> None:
    view = CounterView()
    socket = UnconnectedSocket[CounterState]()
    await view.mount(socket, {}, beam=services(socket))
    await view.handle_event("increment", socket, amount, beam=services(socket))
    assert socket.context.error == "Amount must be an integer from 1 to 10."
    assert socket.context.amount == 1
    assert (await client.get("/counter")).text == "0"


async def test_http_mutation__should_not_exist(client: AsyncClient) -> None:
    assert (await client.post("/increment", data={"amount": "10"})).status_code == 404


async def test_root_template__should_escape_untrusted_attribute_values() -> None:
    page = root_template(
        {
            "id": '"><script>',
            "content": "<p>safe rendered content</p>",
            "csrf_token": '"><script>',
            "session": '"><script>',
            "title": None,
            "additional_head_elements": [],
        }
    )
    assert '"><script>' not in page
    assert "&lt;script&gt;" in page


@pytest.mark.parametrize("fail_output", [False, True])
async def test_actual_socket__should_render_tstrings_and_expose_swallowed_output_limitation(
    client: AsyncClient, *, fail_output: bool
) -> None:
    assert (await client.get("/health")).status_code == 200
    output: asyncio.Queue[str] = asyncio.Queue(maxsize=2)

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        if message["type"] == "websocket.send":
            text = message.get("text")
            assert isinstance(text, str)
            output.put_nowait(text)
            if fail_output:
                error = "transport output failed"
                raise OSError(error)

    websocket = WebSocket(
        {
            "type": "websocket",
            "path": "/",
            "headers": [],
            "extensions": {"hornbeam.owner": "actual-pyview-socket"},
        },
        receive,
        send,
    )
    await websocket.accept()
    view = CounterView()
    socket = ConnectedLiveViewSocket[CounterState](
        websocket, "lv:test", view, AsyncIOScheduler(), NoOpInstrumentation()
    )

    def handler(beam: Services = Depends(services)) -> Services:
        return beam

    context = BindContext(Params({}), None, None, socket, None)
    result = await Binder[CounterState]().abind(handler, context)
    assert result.success, result.errors
    assert result.bound_args["beam"] is services.services
    del websocket.scope["extensions"]
    assert services(socket) is services.services
    with pytest.raises(RuntimeError, match="scope has no Hornbeam owner"):
        await services.subscribe(socket, "demo:counter", counter_event)
    websocket.scope["extensions"] = {"hornbeam.owner": "actual-pyview-socket"}
    beam = services.services
    await view.mount(socket, {}, beam=services(socket))
    assert isinstance(beam, Beam)
    assert ("subscribe", ["actual-pyview-socket", "demo:counter", 65536]) in beam.state.calls
    try:
        await view.handle_event("amount", socket, "10", beam=services(socket))
        assert await beam.incr("shared", 7, namespace="demo") == 7
        assert await beam.publish("demo:counter", b"stale or arbitrary invalidation") == 1
        async with asyncio.timeout(1):
            frame = await output.get()
        assert '"diff"' in frame
        assert '"7"' in frame
        assert socket.context.count == 7
        assert socket.context.amount == 10
        assert view.subscription is not None
        observer = asyncio.create_task(view.subscription.wait())
        await asyncio.sleep(0)
        assert not observer.done()
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert await beam.subscription_count() == 1
    finally:
        await view.disconnect(socket)
    assert await beam.subscription_count() == 0


@pytest.mark.parametrize("payload", [b"changed", b"", b"1.0", b"-", b"\xff", b"1<script>"])
def test_counter_event__should_ignore_notification_payload(payload: bytes) -> None:
    event = counter_event(payload)
    assert event.name == "demo:counter"
    assert event.payload is None
