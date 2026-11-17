import asyncio
import os
import time
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Depends, FastAPI
from pydantic import TypeAdapter, ValidationError

from examples.chat_load.faults import FaultRequest, RoomFault, trigger
from examples.chat_load.origin import require_same_origin
from examples.chat_load.protocol import Ack, Fault, JobDone, Message, Push
from examples.chat_load.rooms import Hub
from examples.chat_load.traffic import Latency, Tally, summarize, usage


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 200),
        ({"origin": "http://test"}, 200),
        ({"origin": "http://other"}, 403),
        ({"origin": "http://test:8800"}, 403),
        ({"origin": "https://test"}, 403),
        ({"origin": "null"}, 403),
        ({"origin": ""}, 403),
        ({"sec-fetch-site": "cross-site"}, 403),
        ({"origin": "http://test", "sec-fetch-site": "same-site"}, 403),
    ],
)
async def test_fault_routes__reject_cross_origin_before_running_faults(
    headers: dict[str, str],
    status: int,
) -> None:
    app = FastAPI()
    calls: list[None] = []

    @app.post("/rooms/{room}/faults/{kind}", dependencies=[Depends(require_same_origin)])
    async def fault() -> Fault:
        calls.append(None)
        return Fault(kind="hog", outcome="completed")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/rooms/room-1/faults/hog", headers=headers)
    assert response.status_code == status
    assert len(calls) == (1 if status == 200 else 0)


def test_faults__raise_hog_and_abort_use_the_requested_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RoomFault, match="Injected room handler failure"):
        trigger(FaultRequest("raise", 0))
    trigger(FaultRequest("hog", 0))
    calls: list[None] = []
    monkeypatch.setattr(os, "abort", lambda: calls.append(None))
    trigger(FaultRequest("abort", 0))
    assert calls == [None]


def test_fault_notice__parses_as_a_push_without_affecting_traffic_counts() -> None:
    adapter: TypeAdapter[Push] = TypeAdapter(Push)
    notice = Fault(kind="raise", outcome="contained")
    assert adapter.validate_json(notice.model_dump_json()) == notice
    tally = Tally()
    tally.receive("room", "client", notice)
    assert tally.pending == 0
    assert tally.errors == {}
    for payload in (b"{}", b'{"type":"fault","kind":"unknown","outcome":"contained"}'):
        with pytest.raises(ValidationError):
            adapter.validate_json(payload)


def test_summarize__reports_percentiles_and_handles_small_samples() -> None:
    assert summarize([]) is None
    assert summarize([2.0]) == Latency(1, 2.0, 2.0, 2.0, 2.0)
    latency = summarize([float(value) for value in range(1, 101)])
    assert latency is not None
    assert (latency.count, latency.p50, latency.p99, latency.max) == (100, 50.5, 99.01, 100.0)


def test_tally__matches_acks_fanout_and_jobs_to_what_was_sent() -> None:
    tally = Tally()
    tally.audience["room"] = 2
    tally.send(("room", "a", 0))
    tally.accept("job", "room", time.perf_counter_ns())
    tally.receive("room", "a", Ack(id=0, count=1))
    tally.receive("room", "b", Message(sender="a", id=0, text="hi", count=1))
    assert (tally.pending, tally.expected) == (2, 2)
    tally.receive("room", "a", Message(sender="a", id=0, text="hi", count=1))
    tally.receive("room", "b", Message(sender="a", id=9, text="hi", count=2))
    tally.receive("room", "b", JobDone(job="job", result=None))
    tally.receive("room", "a", JobDone(job="job", result=None))
    assert tally.acked["room"] == 1
    assert len(tally.fanout_ms) == 2
    assert tally.early == {}
    assert (tally.done["room"], tally.jobs_failed, tally.pending) == (1, 1, 0)
    assert tally.errors == {"unknown message": 1}


def test_tally__counts_a_job_that_finishes_before_its_submission_returns() -> None:
    tally = Tally()
    started = time.perf_counter_ns()
    tally.receive("room", "a", JobDone(job="job", result=7))
    tally.accept("job", "room", started)
    assert (tally.done["room"], tally.jobs_failed, tally.pending, len(tally.job_ms)) == (1, 0, 0, 1)


def test_usage__counts_this_process() -> None:
    if not os.path.isdir("/proc/self/task"):
        pytest.skip("Requires Linux /proc")
    processes, rss, cpu = usage([os.getpid()])
    assert processes >= 1
    assert rss > 0
    assert cpu >= 0


class Feed:
    def __init__(self) -> None:
        self.queue = asyncio.Queue[bytes]()
        self.opened = 0
        self.closed = 0

    @asynccontextmanager
    async def __call__(self, topic: str) -> AsyncGenerator[AsyncIterable[bytes]]:
        self.opened += 1
        try:
            yield self.read()
        finally:
            self.closed += 1

    async def read(self) -> AsyncIterator[bytes]:
        while True:
            yield await self.queue.get()


async def test_hub__shares_one_subscription_and_closes_slow_listeners() -> None:
    feed = Feed()
    async with asyncio.TaskGroup() as tasks:
        hub = Hub(feed, tasks, depth=1)
        async with hub.join("room") as fast, hub.join("room") as slow:
            assert feed.opened == 1
            await feed.queue.put(b"one")
            assert await fast.get() == b"one"
            await feed.queue.put(b"two")
            assert await fast.get() == b"two"
            with pytest.raises(asyncio.QueueShutDown):
                await slow.get()
        await asyncio.sleep(0)
    assert (feed.opened, feed.closed, hub.rooms) == (1, 1, {})


async def test_hub__a_failed_subscription_closes_only_its_room() -> None:
    @asynccontextmanager
    async def broken(topic: str) -> AsyncGenerator[AsyncIterable[bytes]]:
        raise ConnectionError(topic)
        yield Feed().read()

    async with asyncio.TaskGroup() as tasks:
        hub = Hub(broken, tasks)
        async with hub.join("room") as listener:
            with pytest.raises(asyncio.QueueShutDown):
                await listener.get()
    assert hub.rooms == {}
