import asyncio

import httpx
import pytest
from hornbeam.api.errors import SubscriptionClosed as NativeClosed
from hornbeam.api.errors import SubscriptionOverflow as NativeOverflow
from hornbeam.services.client import Beam as NativeBeam

from pybeam.integrations.hornbeam.client import Beam
from pybeam.integrations.hornbeam.errors import SubscriptionClosed, SubscriptionOverflow
from pybeam.integrations.litestar.channels import BeamChannelsBackend, MergeQueueOverflow
from tests.conftest import RunningServer
from tests.fakes import FakeServices, ServiceState


@pytest.mark.runtime
def test_subscription_boundary__native_errors_retirement_and_same_owner_churn(
    service_server: RunningServer,
) -> None:
    response = httpx.post(service_server.base_url + "/subscription-boundary", timeout=30)
    assert response.status_code == 200, response.text
    assert response.json() == {"cycles": 260, "subscriptions": 0}


async def test_native_closed__should_translate_real_exception_identity() -> None:
    subscription = Beam(NativeBeam(owner=object())).subscribe("test")
    with pytest.raises(SubscriptionClosed) as caught:
        await subscription.receive()
    assert isinstance(caught.value.__cause__, NativeClosed)
    assert not isinstance(caught.value, NativeClosed)


async def test_presence_closed__should_translate_real_exception_identity() -> None:
    subscription = Beam(NativeBeam(owner=object())).presence("online", "one", b"A")
    with pytest.raises(SubscriptionClosed) as caught:
        await subscription.receive()
    assert isinstance(caught.value.__cause__, NativeClosed)


async def test_presence_overflow__should_translate_and_untrack() -> None:
    state = ServiceState()
    native = NativeOverflow("presence byte budget exhausted")
    async with Beam(FakeServices(state, owner="socket")).presence("online", "one", b"A") as stream:
        assert await anext(stream)
        next(iter(state.presences.values())).queue.put_nowait(native)
        with pytest.raises(SubscriptionOverflow) as caught:
            await stream.receive()
        assert caught.value.__cause__ is native
    assert not state.presences


@pytest.mark.parametrize("iterate", [False, True])
async def test_native_overflow__should_translate_and_keep_cleanup_ownership(iterate: bool) -> None:
    state = ServiceState()
    native = NativeOverflow("native byte budget exhausted")
    async with Beam(FakeServices(state, owner="test")).subscribe("test") as subscription:
        owned = next(iter(state.channels.values()))
        owned.failure = native
        with pytest.raises(SubscriptionOverflow) as caught:
            if iterate:
                await anext(subscription)
            else:
                await subscription.receive()
        assert caught.value.__cause__ is native
        assert not isinstance(caught.value, MergeQueueOverflow)
    assert not state.channels
    assert not state.subscriptions


async def test_native_overflow__should_remain_distinct_in_litestar() -> None:
    state = ServiceState()
    backend = BeamChannelsBackend(Beam(FakeServices(state, owner="test")))
    await backend.on_startup()
    try:
        await backend.subscribe(["test"])
        owned = next(iter(state.channels.values()))
        native = NativeOverflow("not a local merge queue failure")
        owned.queue.put_nowait(native)
        async with asyncio.timeout(1):
            error = await backend.wait_failure()
        assert isinstance(error, SubscriptionOverflow)
        assert error.__cause__ is native
        assert not isinstance(error, MergeQueueOverflow)
    finally:
        await backend.on_shutdown()
    assert not state.subscriptions
