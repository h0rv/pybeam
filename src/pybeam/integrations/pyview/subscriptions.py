import asyncio
import logging
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Protocol, Self

from pybeam._shared.lifecycle import await_cleanup

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyview.events import InfoEvent

    from pybeam.integrations.hornbeam import PresenceEntry, Services, Subscription

logger = logging.getLogger(__name__)


class InfoReceiver(Protocol):
    async def send_info(self, event: InfoEvent) -> None: ...


class _ViewStream[T]:
    def __init__(
        self,
        stack: AsyncExitStack,
        subscription: Subscription[T],
        socket: InfoReceiver,
        topic: str,
        convert: Callable[[T], InfoEvent],
    ) -> None:
        self._stack = stack
        self._failure: Exception | None = None
        self._closing: asyncio.Task[None] | None = None
        self._task = asyncio.create_task(
            self._consume(subscription, socket, convert), name="pyview:{}".format(topic)
        )
        self._task.add_done_callback(self._report_failure)

    @classmethod
    async def _open(
        cls,
        context: Subscription[T],
        socket: InfoReceiver,
        topic: str,
        convert: Callable[[T], InfoEvent],
    ) -> Self:
        stack = AsyncExitStack()
        try:
            subscription = await context.__aenter__()
            stack.push_async_exit(context.__aexit__)
        except BaseException:
            await stack.aclose()
            raise
        return cls(stack, subscription, socket, topic, convert)

    async def _consume(
        self,
        subscription: Subscription[T],
        socket: InfoReceiver,
        convert: Callable[[T], InfoEvent],
    ) -> None:
        async with self._stack:
            try:
                async for payload in subscription:
                    await socket.send_info(convert(payload))
            except Exception as error:
                self._failure = error
                logger.exception("PyView BEAM subscription failed")
                raise

    def _report_failure(self, task: asyncio.Task[None]) -> None:
        if (
            not task.cancelled()
            and (error := task.exception()) is not None
            and error is not self._failure
        ):
            logger.error("PyView BEAM subscription failed", exc_info=error)

    async def wait(self) -> None:
        """Observe consumer completion without transferring cancellation ownership."""
        await asyncio.shield(self._task)

    async def aclose(self) -> None:
        """Await acknowledged cleanup, even if the disconnect caller is cancelled."""
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(), name="pyview:close")
        await await_cleanup(self._closing)

    async def _close(self) -> None:
        if not self._task.done() and self._failure is None:
            self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            if not self._task.cancelled():
                raise
        finally:
            await self._stack.aclose()


class ViewSubscription(_ViewStream[bytes]):
    """Retain the handle from ``services.subscribe`` and await close on disconnect."""

    @classmethod
    async def start(
        cls,
        beam: Services,
        socket: InfoReceiver,
        topic: str,
        convert: Callable[[bytes], InfoEvent],
        *,
        max_bytes: int = 65536,
    ) -> Self:
        return await cls._open(beam.subscribe(topic, max_bytes=max_bytes), socket, topic, convert)


class ViewPresence(_ViewStream["tuple[PresenceEntry, ...]"]):
    """Receive full snapshots; await close on disconnect to acknowledge untracking.

    Presence is eventually consistent across nodes and is not durable.
    """

    @classmethod
    async def start(
        cls,
        beam: Services,
        socket: InfoReceiver,
        topic: str,
        key: str,
        payload: bytes,
        convert: Callable[[tuple[PresenceEntry, ...]], InfoEvent],
        *,
        max_bytes: int = 65536,
    ) -> Self:
        return await cls._open(
            beam.presence(topic, key, payload, max_bytes=max_bytes), socket, topic, convert
        )
