import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from litestar.channels.backends.base import ChannelsBackend

from pybeam.integrations.hornbeam.errors import SubscriptionClosed


class MergeQueueOverflow(RuntimeError):
    """The local framework merge queue exhausted its item capacity."""


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Coroutine, Iterable

    from litestar.channels import ChannelsPlugin
    from litestar.channels.subscriber import Subscriber

    from pybeam.integrations.hornbeam import Services

MIN_SUBSCRIPTION_BYTES = 1024
MAX_SUBSCRIPTION_BYTES = 1048576


@dataclass(eq=False)
class _Receiver:
    ready: asyncio.Future[None]
    task: asyncio.Task[None] | None = None


class BeamChannelsBackend(ChannelsBackend):
    """Own one native context per channel; duplicate subscriptions are idempotent.

    Overflow or receiver failure ends the stream without raising. Observe errors
    through failure, subscription(), or receive_or_failure(). Failed backends
    cannot restart. Cancelling the stream closes every channel context.
    """

    def __init__(
        self, beam: Services | None = None, *, max_pending: int = 128, max_bytes: int = 65536
    ) -> None:
        if max_pending <= 0:
            message = "max_pending must be positive"
            raise ValueError(message)
        if not MIN_SUBSCRIPTION_BYTES <= max_bytes <= MAX_SUBSCRIPTION_BYTES:
            message = "max_bytes must be between 1024 and 1048576"
            raise ValueError(message)
        self._beam = beam
        self._max_bytes = max_bytes
        self._queue: asyncio.Queue[tuple[str, _Receiver, bytes]] = asyncio.Queue(max_pending)
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._receivers: dict[str, _Receiver] = {}
        self._running = False
        self._streaming = False
        self._failure: Exception | None = None
        self._failed = asyncio.Event()

    @property
    def failure(self) -> Exception | None:
        """Terminal receiver/overflow failure, including when no stream is running."""
        return self._failure

    def _client(self) -> Services:
        if self._failure is not None:
            raise self._failure
        if not self._running or self._beam is None:
            message = "BeamChannelsBackend is not started"
            raise RuntimeError(message)
        return self._beam

    def _fail(self, error: Exception) -> None:
        if self._failure is None:
            self._failure = error
        self._failed.set()
        self._wake.set()

    async def wait_failure(self) -> Exception:
        """Wait for the retained terminal error without throwing in a worker task."""
        await self._failed.wait()
        if self._failure is None:
            message = "failure event set without a terminal error"
            raise RuntimeError(message)
        return self._failure

    async def receive_or_failure[T](self, receive: Coroutine[object, object, T]) -> T:
        """Race one owned read against terminal failure; cancel and await both tasks."""
        reading = asyncio.create_task(receive)
        failed = asyncio.create_task(self.wait_failure())
        try:
            await asyncio.wait((reading, failed), return_when=asyncio.FIRST_COMPLETED)
            if self._failure is not None:
                raise self._failure
            return reading.result()
        finally:
            reading.cancel()
            failed.cancel()
            for task in (reading, failed):
                with suppress(asyncio.CancelledError):
                    await task

    @asynccontextmanager
    async def subscription(
        self, plugin: ChannelsPlugin, channels: str | Iterable[str]
    ) -> AsyncGenerator[AsyncGenerator[bytes]]:
        """Own a subscriber and drain it on exit; propagate terminal failures.

        Do not consume the subscriber elsewhere or start its background sender.
        """
        subscriber = await plugin.subscribe(channels)
        events = subscriber.iter_events()

        async def messages() -> AsyncGenerator[bytes]:
            while True:
                try:
                    yield await self.receive_or_failure(anext(events))
                except StopAsyncIteration:
                    return

        messages_iterator = messages()
        try:
            yield messages_iterator
        finally:
            await messages_iterator.aclose()
            await events.aclose()
            await self._close_subscriber(plugin, subscriber)

    @staticmethod
    async def _close_subscriber(plugin: ChannelsPlugin, subscriber: Subscriber) -> None:
        await subscriber.stop(join=False)
        remaining = subscriber.iter_events()
        try:
            while subscriber.qsize:
                await anext(remaining)
        finally:
            await remaining.aclose()
        await plugin.unsubscribe(subscriber)

    @override
    async def on_startup(self) -> None:
        async with self._lock:
            if self._failure is not None:
                raise self._failure
            if self._beam is None:
                from pybeam.integrations.hornbeam.client import Beam

                self._beam = await Beam.connect()
            self._running = True

    async def _receive(self, beam: Services, channel: str, receiver: _Receiver) -> None:
        try:
            async with beam.subscribe(channel, max_bytes=self._max_bytes) as subscription:
                receiver.ready.set_result(None)
                async for payload in subscription:
                    try:
                        self._queue.put_nowait((channel, receiver, payload))
                    except asyncio.QueueFull:
                        message = "Litestar backend merge queue is full"
                        raise MergeQueueOverflow(message) from None
                    self._wake.set()
                self._fail(SubscriptionClosed("Litestar backend channel closed: " + channel))
        except asyncio.CancelledError:
            if not receiver.ready.done():
                receiver.ready.cancel()
            raise
        except Exception as exc:
            self._fail(exc)
            if not receiver.ready.done():
                receiver.ready.set_exception(exc)

    async def _stop(self, receivers: Iterable[_Receiver]) -> None:
        owned = tuple(receivers)
        for receiver in owned:
            if receiver.task is not None:
                receiver.task.cancel()
        for receiver in owned:
            if receiver.task is not None:
                with suppress(asyncio.CancelledError):
                    await receiver.task

    @override
    async def subscribe(self, channels: Iterable[str]) -> None:
        names = tuple(dict.fromkeys(channels))
        async with self._lock:
            beam = self._client()
            added: list[str] = []
            try:
                for channel in names:
                    if channel in self._receivers:
                        continue
                    receiver = _Receiver(asyncio.get_running_loop().create_future())
                    self._receivers[channel] = receiver
                    added.append(channel)
                    receiver.task = asyncio.create_task(self._receive(beam, channel, receiver))
                    await receiver.ready
            except BaseException:
                await self._stop(self._receivers.pop(channel) for channel in added)
                raise

    @override
    async def unsubscribe(self, channels: Iterable[str]) -> None:
        names = tuple(dict.fromkeys(channels))
        async with self._lock:
            removed = [self._receivers.pop(name) for name in names if name in self._receivers]
            await self._stop(removed)

    @override
    async def publish(self, data: bytes, channels: Iterable[str]) -> None:
        """Send through native receivers; retain failures without raising.

        wait_published() does not acknowledge delivery. Inspect failure instead.
        """
        if self._failure is not None:
            return
        beam = self._client()
        try:
            for channel in tuple(dict.fromkeys(channels)):
                await beam.publish(channel, data)
        except Exception as exc:
            self._fail(exc)

    @override
    async def stream_events(self) -> AsyncGenerator[tuple[str, bytes]]:
        if self.failure is not None:
            return
        self._client()
        if self._streaming:
            message = "only one Litestar backend stream is supported"
            raise RuntimeError(message)
        self._streaming = True
        try:
            while self._running:
                if self._failure is not None:
                    return
                try:
                    channel, receiver, payload = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                if self._receivers.get(channel) is receiver:
                    yield channel, payload
        finally:
            self._streaming = False
            await self.on_shutdown()

    @override
    async def on_shutdown(self) -> None:
        async with self._lock:
            self._running = False
            self._wake.set()
            receivers = tuple(self._receivers.values())
            self._receivers.clear()
            await self._stop(receivers)
            while not self._queue.empty():
                self._queue.get_nowait()

    @override
    async def get_history(self, channel: str, limit: int | None = None) -> list[bytes]:
        return []
