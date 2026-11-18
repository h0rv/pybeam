import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Self

from pybeam._shared.lifecycle import await_cleanup
from pybeam.integrations.hornbeam.errors import (
    SubscriptionClosed,
    SubscriptionOverflow,
)

if TYPE_CHECKING:
    from pybeam.integrations.hornbeam import PresenceEntry


@dataclass
class Entry:
    data: bytes
    deadline: float | None


class ServiceState:
    def __init__(self, *, capacity: int = 32) -> None:
        self.capacity = capacity
        self.now = 0
        self.calls: list[tuple[str, list[object]]] = []
        self.channels: dict[object, FakeSubscription] = {}
        self.subscriptions: dict[object, tuple[str, FakeSubscription]] = {}
        self.presences: dict[object, FakePresence] = {}
        self.cache: dict[tuple[str, str], Entry] = {}
        self.counters: dict[tuple[str, str], int] = {}
        self.published: list[tuple[str, bytes]] = []
        self.opened = 0
        self.update_during_snapshot = False
        self.close_started = asyncio.Event()
        self.close_gate = asyncio.Event()
        self.close_gate.set()


class FakeSubscription:
    def __init__(self, state: ServiceState, owner: object, topic: str, max_bytes: int) -> None:
        self.state = state
        self.owner = owner
        self.topic = topic
        self.max_bytes = max_bytes
        self.queue: asyncio.Queue[bytes | Exception] = asyncio.Queue(state.capacity)
        self.waiting = asyncio.Event()
        self.entered = self.waiting
        self.closed = False
        self.failure: Exception | None = None
        self.cleanup: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self.state.calls.append(("subscribe", [self.owner, self.topic, self.max_bytes]))
        self.state.channels[self] = self
        self.state.subscriptions[self] = self.topic, self
        self.state.opened += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self.receive()
        except SubscriptionClosed:
            raise StopAsyncIteration from None

    async def receive(self) -> bytes:
        if self.failure is not None:
            raise self.failure
        if self.closed:
            raise SubscriptionClosed("closed")
        self.waiting.set()
        value = await self.queue.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def _close(self) -> None:
        self.state.close_started.set()
        await self.state.close_gate.wait()
        self.state.calls.append(("unsubscribe", [self.owner, self.topic]))
        self.state.channels.pop(self, None)
        self.state.subscriptions.pop(self, None)
        self.closed = True

    async def aclose(self) -> None:
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
        await await_cleanup(self.cleanup)


class FakePresence:
    def __init__(
        self,
        state: ServiceState,
        owner: object,
        topic: str,
        key: str,
        payload: bytes,
        max_bytes: int,
    ) -> None:
        self.state, self.owner, self.topic = state, owner, topic
        self.key, self.payload, self.max_bytes = key, payload, max_bytes
        self.queue: asyncio.Queue[tuple[PresenceEntry, ...] | Exception] = asyncio.Queue(8)
        self.cleanup: asyncio.Task[None] | None = None
        self.closed = False

    def _broadcast(self) -> None:
        from pybeam.integrations.hornbeam import PresenceEntry

        snapshot = tuple(
            PresenceEntry(item.key, item.key, item.payload)
            for item in self.state.presences.values()
            if item.topic == self.topic
        )
        for item in self.state.presences.values():
            if item.topic == self.topic:
                item.queue.put_nowait(snapshot)

    async def __aenter__(self) -> Self:
        self.state.calls.append(("presence", [self.owner, self.topic, self.key, self.max_bytes]))
        self.state.presences[self] = self
        self._broadcast()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> tuple[PresenceEntry, ...]:
        if self.closed:
            raise StopAsyncIteration
        return await self.receive()

    async def receive(self) -> tuple[PresenceEntry, ...]:
        if self.closed:
            raise SubscriptionClosed("closed")
        value = await self.queue.get()
        if isinstance(value, Exception):
            raise value
        return value

    async def _close(self) -> None:
        self.state.close_started.set()
        await self.state.close_gate.wait()
        self.state.presences.pop(self, None)
        self.closed = True
        self._broadcast()

    async def aclose(self) -> None:
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
        await await_cleanup(self.cleanup)


class FakeStore:
    def __init__(self, state: ServiceState, namespace: str) -> None:
        self.state = state
        self.namespace = namespace

    def _entry(self, key: str) -> Entry | None:
        identity = self.namespace, key
        entry = self.state.cache.get(identity)
        if entry is not None and entry.deadline is not None and self.state.now >= entry.deadline:
            self.state.cache.pop(identity)
            return None
        return entry

    @staticmethod
    def _duration(seconds: float | None) -> None:
        if seconds is not None and (not math.isfinite(seconds) or seconds < 0):
            raise ValueError("duration must be finite and nonnegative")

    async def set(self, key: str, value: bytes, *, expires_in: float | None = None) -> None:
        self._duration(expires_in)
        deadline = None if expires_in is None else self.state.now + expires_in * 1000
        self.state.cache[self.namespace, key] = Entry(value, deadline)

    async def get(self, key: str, *, renew_for: float | None = None) -> bytes | None:
        self._duration(renew_for)
        entry = self._entry(key)
        if entry is None:
            return None
        if renew_for is not None and entry.deadline is not None:
            entry.deadline = self.state.now + renew_for * 1000
        return entry.data

    async def delete(self, key: str) -> None:
        self.state.cache.pop((self.namespace, key), None)

    async def delete_all(self) -> None:
        self.state.cache = {
            key: entry for key, entry in self.state.cache.items() if key[0] != self.namespace
        }

    async def exists(self, key: str) -> bool:
        return self._entry(key) is not None

    async def expires_in(self, key: str) -> float | None:
        entry = self._entry(key)
        if entry is None or entry.deadline is None:
            return None
        return (entry.deadline - self.state.now) / 1000


class FakeServices:
    def __init__(self, state: ServiceState, *, owner: object) -> None:
        self.state = state
        self.owner = owner

    def for_scope(self, scope: Mapping[str, object]) -> FakeServices:
        match scope:
            case {"extensions": {"hornbeam.owner": owner}} if owner is not None:
                return FakeServices(self.state, owner=owner)
            case _:
                raise RuntimeError("scope has no Hornbeam owner")

    def store(self, namespace: str) -> FakeStore:
        return FakeStore(self.state, namespace)

    async def incr(self, key: str, delta: int = 1, *, namespace: str = "default") -> int:
        self.state.calls.append(("counter_incr", [namespace, key, delta]))
        identity = namespace, key
        value = self.state.counters.get(identity, 0) + delta
        self.state.counters[identity] = value
        if self.state.update_during_snapshot:
            self.state.update_during_snapshot = False
            self.state.counters[identity] += 1
            await self.publish("demo:counter", str(value + 1).encode("ascii"))
        return value

    async def publish(self, topic: str, payload: bytes) -> int:
        self.state.calls.append(("publish", [topic, payload]))
        self.state.published.append((topic, payload))
        count = 0
        for subscription in self.state.channels.values():
            if subscription.topic == topic:
                count += 1
                try:
                    subscription.queue.put_nowait(payload)
                except asyncio.QueueFull:
                    subscription.failure = SubscriptionOverflow("subscription queue full")
        return count

    def subscribe(self, topic: str, *, max_bytes: int = 65536) -> FakeSubscription:
        return FakeSubscription(self.state, self.owner, topic, max_bytes)

    async def subscription_count(self) -> int:
        return len(self.state.channels) + len(self.state.presences)

    def presence(
        self, topic: str, key: str, payload: bytes, *, max_bytes: int = 65536
    ) -> FakePresence:
        return FakePresence(self.state, self.owner, topic, key, payload, max_bytes)
