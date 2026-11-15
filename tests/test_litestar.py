import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from litestar import Litestar
from litestar.channels import ChannelsPlugin
from litestar.testing import AsyncTestClient, RequestFactory

from examples.litestar import main as litestar_app
from examples.litestar.main import (
    TOPIC,
    CounterEvents,
    counter,
    events,
    health,
    increment,
    index,
    template_config,
)
from pybeam.errors import SubscriptionOverflow
from pybeam.litestar.channels import BeamChannelsBackend
from pybeam.litestar.store import BeamStore
from tests.fakes import FakeServices as Beam
from tests.fakes import ServiceState as FakeBridge

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterable

    from litestar.channels.subscriber import Subscriber
    from litestar.stores.base import Store
    from litestar.types import HTTPDisconnectEvent, Message

    from pybeam.services import Subscription


def setup_backend(*, max_pending: int = 128) -> tuple[FakeBridge, BeamChannelsBackend]:
    bridge = FakeBridge()
    beam = Beam(bridge, owner="explicit-test-owner")
    return bridge, BeamChannelsBackend(beam, max_pending=max_pending)


async def eventually(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0)


async def test_store__should_encode_and_isolate_exact_namespaces() -> None:
    bridge = FakeBridge()
    beam = Beam(bridge, owner="test")
    store: Store = BeamStore(beam, "a:b")
    other: Store = BeamStore(beam, "a")
    await store.set("c", "café")
    await other.set("b:c", b"other")
    assert await store.get("c") == "café".encode()
    assert await store.expires_in("c") is None
    assert await store.get("c", renew_for=5) == "café".encode()
    assert await store.expires_in("c") is None
    await store.delete_all()
    assert await store.get("c") is None
    assert await other.get("b:c") == b"other"
    await store.delete("missing")
    assert not await store.exists("missing")


async def test_store__should_preserve_ttl_and_atomic_renewal_contract() -> None:
    bridge = FakeBridge()
    store: Store = BeamStore(Beam(bridge, owner="test"), "test")
    await store.set("k", b"v", timedelta(milliseconds=1250))
    assert await store.expires_in("k") == 1
    bridge.now = 1000
    assert await store.expires_in("k") == 0
    assert await store.get("k", timedelta(seconds=2)) == b"v"
    assert await store.expires_in("k") == 2
    bridge.now = 3000
    assert not await store.exists("k")
    assert await store.get("k", 5) is None
    assert await store.expires_in("k") is None
    await store.set("zero", b"v", 0)
    assert await store.get("zero") is None
    await store.set("renew-zero", b"v", 5)
    assert await store.get("renew-zero", 0) == b"v"
    assert not await store.exists("renew-zero")
    for duration in (-1, timedelta(microseconds=-1)):
        with pytest.raises(ValueError, match="nonnegative"):
            await store.set("bad", b"v", duration)
        with pytest.raises(ValueError, match="nonnegative"):
            await store.get("bad", duration)


async def test_plugin_lifecycle__should_deduplicate_and_fan_out_only_native_delivery() -> None:
    bridge, backend = setup_backend()
    plugin = ChannelsPlugin(backend, channels=["demo:counter"], subscriber_max_backlog=4)
    async with asyncio.timeout(2), plugin:
        assert len(bridge.subscriptions) == 1
        async with (
            plugin.start_subscription("demo:counter") as first,
            plugin.start_subscription("demo:counter") as second,
        ):
            assert bridge.opened == 1
            await backend.publish(b"1", ["demo:counter"])
            first_events = first.iter_events()
            second_events = second.iter_events()
            assert await anext(first_events) == b"1"
            assert await anext(second_events) == b"1"
            assert first.qsize == second.qsize == 0
            await first_events.aclose()
            await second_events.aclose()
            assert bridge.published == [("demo:counter", b"1")]
            assert await backend.get_history("demo:counter", 10) == []
        assert not bridge.subscriptions
    assert not bridge.subscriptions


async def test_plugin_shutdown__should_close_receivers_when_stream_cancelled() -> None:
    bridge, backend = setup_backend()
    plugin = ChannelsPlugin(backend, channels=["one", "two"])
    async with asyncio.timeout(2), plugin:
        assert len(bridge.subscriptions) == 2
        await asyncio.sleep(0)
    assert not bridge.subscriptions


async def test_backend__should_materialize_generators_and_filter_old_subscriptions() -> None:
    bridge, backend = setup_backend()
    await backend.on_startup()
    await backend.subscribe(name for name in ("one", "one", "two"))
    assert bridge.opened == 2
    await backend.publish(b"old", iter(["one", "one"]))
    await eventually(next(iter(bridge.subscriptions.values()))[1].queue.empty)
    await backend.unsubscribe(iter(["one", "one"]))
    await backend.subscribe(["one"])
    await backend.publish(b"new", ["one"])
    stream = backend.stream_events()
    async with asyncio.timeout(1):
        assert await anext(stream) == ("one", b"new")
    await stream.aclose()
    assert not bridge.subscriptions
    assert bridge.published == [("one", b"old"), ("one", b"new")]


async def test_overflow__should_be_observable_and_shutdown_without_queue_space() -> None:
    bridge, backend = setup_backend(max_pending=1)
    await backend.on_startup()
    await backend.subscribe(["one"])
    await backend.publish(b"1", ["one"])
    await backend.publish(b"2", ["one"])
    await eventually(lambda: backend.failure is not None)
    assert isinstance(backend.failure, SubscriptionOverflow)
    async with asyncio.timeout(1):
        await backend.on_shutdown()
        with pytest.raises(SubscriptionOverflow, match="merge queue"):
            await backend.receive_or_failure(asyncio.sleep(0))
    assert not bridge.subscriptions


async def test_cancelled_stream__should_not_deadlock_with_full_merge_queue() -> None:
    bridge, backend = setup_backend(max_pending=1)
    await backend.on_startup()
    await backend.subscribe(["one"])
    stream = backend.stream_events()
    waiting = asyncio.create_task(anext(stream))
    await backend.publish(b"1", ["one"])
    assert await waiting == ("one", b"1")
    await backend.publish(b"2", ["one"])
    channel = next(iter(bridge.subscriptions.values()))[1]
    await eventually(channel.queue.empty)
    async with asyncio.timeout(1):
        await stream.aclose()
        await backend.on_shutdown()
    assert not bridge.subscriptions


async def test_receiver_failure__should_surface_and_cleanup_full_plugin_lifecycle() -> None:
    bridge, backend = setup_backend()
    plugin = ChannelsPlugin(backend, channels=["one"])
    before = asyncio.all_tasks()
    async with asyncio.timeout(2), plugin, backend.subscription(plugin, "one") as events:
        waiting = asyncio.create_task(anext(events))
        channel = next(iter(bridge.subscriptions.values()))[1]
        channel.queue.put_nowait(RuntimeError("service receive failed"))
        with pytest.raises(RuntimeError, match="service receive failed"):
            await waiting
        await plugin.wait_published(b"after failure", "one")
        await eventually(lambda: not bridge.subscriptions)
    assert isinstance(backend.failure, RuntimeError)
    assert not bridge.subscriptions
    assert not (asyncio.all_tasks() - before)


async def test_example_http__should_validate_form_origin_and_use_real_adapters() -> None:
    bridge = FakeBridge()
    beam = Beam(bridge, owner="test-app")
    app = Litestar(
        route_handlers=[
            health,
            index,
            counter,
            increment,
        ],
        template_config=template_config,
        stores={"demo": BeamStore(beam, "litestar:demo")},
    )
    previous = litestar_app.beam
    litestar_app.beam = beam
    try:
        async with (
            asyncio.timeout(3),
            AsyncTestClient(app=app, base_url="http://testserver.local") as client,
        ):
            await app.stores.get("demo").set("notice", "<script>alert(1)</script>")
            assert (await client.get("/health")).status_code == 200
            page = await client.get("/")
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
            assert "<script>alert(1)</script>" not in page.text
            for invalid in ("0", "11", "bad"):
                response = await client.post("/increment", data={"amount": invalid})
                assert response.status_code == 400
            for headers in (
                {"origin": "https://attacker.example"},
                {"origin": "null"},
                {"sec-fetch-site": "cross-site"},
                {"sec-fetch-site": "same-site"},
            ):
                response = await client.post("/increment", data={"amount": "1"}, headers=headers)
                assert response.status_code == 403, response.text
            assert not bridge.published
            bridge.calls.clear()
            response = await client.post(
                "/increment", data={"amount": "2"}, headers={"origin": "http://testserver.local"}
            )
            assert response.status_code == 200
            assert response.text == "Added 2."
            assert [args for action, args in bridge.calls if action == "counter_incr"] == [
                ["demo", "shared", 2]
            ]
            assert (await client.get("/counter")).text == '<span id="counter">2</span>'
            assert bridge.published == [("demo:counter", b"changed")]
            assert 'id="counter-region" hx-sse:connect="/events" hx-swap="innerHTML"' in page.text
            assert 'hx-target="#confirmation" hx-swap="innerHTML"' in page.text
            assert 'id="confirmation" role="status"' in page.text
            assert 'hx-target="#counter-region"' not in page.text
            assert 'hx-target="#counter"' not in page.text
            assert "outerHTML" not in page.text
            assert "hx-ext" not in page.text
            assert "sse-swap" not in page.text
            assert 'rel="icon" href="data:,"' in page.text
            assert bridge.counters == {("demo", "shared"): 2}
    finally:
        litestar_app.beam = previous
    assert not bridge.subscriptions


async def test_stream_cancellation__should_close_native_contexts() -> None:
    bridge, backend = setup_backend()
    await backend.on_startup()
    await backend.subscribe(["one", "two"])
    stream = backend.stream_events()
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not bridge.subscriptions
    await backend.on_shutdown()


async def test_backend__should_reject_use_before_startup() -> None:
    _, backend = setup_backend()
    with pytest.raises(RuntimeError, match="not started"):
        await backend.subscribe(["one"])
    with pytest.raises(RuntimeError, match="not started"):
        await backend.publish(b"x", ["one"])


async def test_helper__should_drain_a_full_subscriber_without_blocking_unsubscribe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, backend = setup_backend()
    plugin = ChannelsPlugin(backend, channels=["one"], subscriber_max_backlog=1)
    subscribers: list[Subscriber] = []
    original = plugin.subscribe

    async def subscribe(channels: str | Iterable[str], history: int | None = None) -> Subscriber:
        subscriber = await original(channels, history)
        subscribers.append(subscriber)
        return subscriber

    monkeypatch.setattr(plugin, "subscribe", subscribe)
    before = asyncio.all_tasks()
    async with asyncio.timeout(2), plugin:
        async with backend.subscription(plugin, "one") as events:
            await backend.publish(b"first", ["one"])
            assert await anext(events) == b"first"
            await backend.publish(b"full", ["one"])
            await eventually(lambda: subscribers[0].qsize == 1)
            assert not subscribers[0].is_running
        assert not bridge.subscriptions
    assert not (asyncio.all_tasks() - before)


async def test_response__should_close_request_subscription_on_blocked_send_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = FakeBridge()
    beam = Beam(bridge, owner="request-owner")
    monkeypatch.setattr(litestar_app, "beam", beam)
    app = Litestar(route_handlers=[events])
    request = RequestFactory(app=app).get("/events")
    request.scope["extensions"] = {"hornbeam.owner": {"test-owner": "request"}}
    blocked = asyncio.Event()
    retained: list[AsyncGenerator[bytes]] = []
    original = CounterEvents.messages

    def track_messages(
        response: CounterEvents, subscription: Subscription
    ) -> AsyncGenerator[bytes]:
        messages = original(response, subscription)
        retained.append(messages)
        return messages

    monkeypatch.setattr(CounterEvents, "messages", track_messages)

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            blocked.set()
            await asyncio.Event().wait()

    async def receive() -> HTTPDisconnectEvent:
        await blocked.wait()
        return {"type": "http.disconnect"}

    async with asyncio.timeout(2):
        await app(request.scope, receive, send)
    assert blocked.is_set()
    assert len(retained) == 1
    assert not bridge.subscriptions
    assert bridge.opened == 1


async def test_response__should_reload_state_for_reordered_notifications() -> None:
    bridge = FakeBridge()
    beam = Beam(bridge, owner="request-owner")
    response = CounterEvents(beam)
    bodies: list[bytes] = []
    finished = asyncio.Event()

    async def send(message: Message) -> None:
        if message["type"] != "http.response.body":
            return
        bodies.append(message.get("body", b""))
        if len(bodies) == 1:
            await beam.incr("shared", 2, namespace="demo")
            await beam.publish(TOPIC, b"2")
            await beam.publish(TOPIC, b"1")
        elif len(bodies) == 3:
            finished.set()

    async def receive() -> HTTPDisconnectEvent:
        await finished.wait()
        return {"type": "http.disconnect"}

    async with asyncio.timeout(2):
        await response(RequestFactory().get("/events").scope, receive, send)
    assert len(bodies) == 3
    assert all(b'<span id="counter">2</span>' in body for body in bodies[1:])
    assert all(b"event:" not in body for body in bodies)
    assert not bridge.subscriptions


async def test_backend__should_race_failure_with_idle_native_read() -> None:
    bridge, backend = setup_backend()
    beam = Beam(bridge, owner="test-app")
    plugin = ChannelsPlugin(backend, channels=[TOPIC])
    before = asyncio.all_tasks()

    async with asyncio.timeout(2), plugin:
        receiver = next(iter(bridge.channels.values()))
        with pytest.raises(ExceptionGroup, match="TaskGroup") as caught:
            async with beam.subscribe(TOPIC) as subscription, asyncio.TaskGroup() as group:
                waiting = group.create_task(backend.receive_or_failure(subscription.receive()))
                await subscription.waiting.wait()
                assert not waiting.done()
                receiver.queue.put_nowait(RuntimeError("service receive failed"))
                await waiting
        assert len(caught.value.exceptions) == 1
        error = caught.value.exceptions[0]
        assert isinstance(error, RuntimeError)
        assert str(error) == "service receive failed"
        assert not bridge.subscriptions
    assert not (asyncio.all_tasks() - before)
