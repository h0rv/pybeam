import asyncio
import math
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from inspect import iscoroutinefunction
from pathlib import Path
from types import FunctionType

from pybeam.runtime.config import load_config

try:
    import erlang
except ModuleNotFoundError as error:
    if error.name != "erlang":
        raise
    erlang = None


class QueueStoreError(RuntimeError):
    """The store failed; an interrupted write may still have committed."""


class QueueFull(QueueStoreError):
    """The queue's limit includes queued and leased items."""


class LeaseExpired(QueueStoreError):
    """The lease expired or another delivery owns the item."""


class PayloadTooLarge(ValueError):
    """Payloads cannot exceed 1 MiB."""


@dataclass(frozen=True, slots=True)
class QueueItem:
    """A delivery's bytes and attempt number, starting at one."""

    payload: bytes
    attempt: int


@dataclass(frozen=True, slots=True)
class _Claim:
    id: int
    token: bytes
    item: QueueItem


async def _call(name: str, *args: object) -> object:
    if erlang is None:
        raise QueueStoreError("DurableQueue requires the managed runtime")
    try:
        result = await erlang.async_call(name, *args)
    except Exception as error:
        raise QueueStoreError("Queue operation failed: " + name) from error
    if isinstance(result, tuple) and len(result) == 2 and result[0] == "error":
        reason: object = result[1]
        if reason == "full":
            raise QueueFull("Queue is full")
        if reason == "stale_lease":
            raise LeaseExpired("Lease no longer belongs to this delivery")
        if reason == "payload_too_large":
            raise PayloadTooLarge("Payload exceeds 1 MiB")
        raise QueueStoreError("Queue operation failed: " + repr(reason))
    return result


def _claim(result: object) -> _Claim | None:
    if result == "empty":
        return None
    if isinstance(result, tuple) and len(result) == 5:
        status, item_id, token, attempt, payload = result
        if (
            status == "ok"
            and type(item_id) is int
            and isinstance(token, bytes)
            and type(attempt) is int
            and attempt > 0
            and isinstance(payload, bytes)
        ):
            return _Claim(item_id, token, QueueItem(payload, attempt))
    raise QueueStoreError("Invalid claim response")


async def _finish[T](task: asyncio.Task[T]) -> T:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _settle(task: asyncio.Task[object]) -> None:
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        await _finish(task)
        raise
    if result != "ok":
        raise QueueStoreError("Invalid write response")


def _lease_ms(lease: float) -> int:
    if isinstance(lease, bool) or not math.isfinite(lease) or not 0 < lease <= 4_294_967:
        raise ValueError("lease must be finite and positive, at most 4294967 seconds")
    return max(1, math.ceil(lease * 1000))


def _stored(result: object) -> bool:
    if result == ("ok", "stored"):
        return True
    if result == ("ok", "duplicate"):
        return False
    raise QueueStoreError("Invalid put response")


class DurableQueue:
    """At-least-once prototype; same-host process-crash tested.

    Keys are retained until ack, not forever. Keep the data directory across
    restarts. Empty replicas means this node only. Replicated Mnesia without
    majority is not partition-safe. This is not production-ready.
    """

    def __init__(self, name: str, *, max_items: int = 10_000, replicas: Sequence[str] = ()) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("Queue name must be nonempty text")
        if type(max_items) is not int or max_items < 1:
            raise ValueError("max_items must be a positive integer")
        if isinstance(replicas, str | bytes) or any(
            not node or "\x00" in node for node in replicas
        ):
            raise ValueError("replicas must contain nonempty node names")
        self._name = name.encode()
        self._max_items = max_items
        self._replicas = tuple(node.encode() for node in replicas)
        self._directory = load_config(Path.cwd()).data_dir
        self._opened = False

    async def _open(self) -> None:
        if not self._opened:
            result = await _call(
                "_queue_open",
                str(self._directory),
                self._name,
                self._max_items,
                list(self._replicas),
            )
            if result != "ok":
                raise QueueStoreError("Invalid open response")
            self._opened = True

    async def put(self, payload: bytes, *, key: str | None = None) -> bool:
        """Sync the write before returning True; return False for a retained key.

        Cancellation is uncertain: the item may have been stored. Retry with
        the same key to avoid inserting it twice while that key is retained.
        """
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if len(payload) > 1_048_576:
            raise PayloadTooLarge("Payload exceeds 1 MiB")
        if key is not None and not isinstance(key, str):
            raise TypeError("key must be text or None")
        await self._open()
        result = await _call(
            "_queue_put", self._name, None if key is None else key.encode(), payload
        )
        return _stored(result)

    async def _receive(self, lease_ms: int) -> _Claim:
        while True:
            pending = asyncio.create_task(_call("_queue_claim", self._name, lease_ms, 250))
            try:
                result = await asyncio.shield(pending)
            except asyncio.CancelledError:
                late = _claim(await _finish(pending))
                if late is not None:
                    with suppress(LeaseExpired):
                        await _finish(
                            asyncio.create_task(_call("_queue_release", late.id, late.token))
                        )
                raise
            delivery = _claim(result)
            if delivery is not None:
                return delivery

    @asynccontextmanager
    async def get(self, *, lease: float = 30.0) -> AsyncGenerator[QueueItem]:
        """Wait for a delivery; ack on clean exit, release on error.

        Expired acks raise LeaseExpired. Cancellation while waiting releases
        late claims before returning and may take a further 250 ms plus sync.
        """
        lease_ms = _lease_ms(lease)
        await self._open()
        delivery = await self._receive(lease_ms)
        try:
            yield delivery.item
        except BaseException:
            with suppress(LeaseExpired):
                await _settle(
                    asyncio.create_task(_call("_queue_release", delivery.id, delivery.token))
                )
            raise
        else:
            await _settle(asyncio.create_task(_call("_queue_ack", delivery.id, delivery.token)))


class DurableTasks:
    """At-least-once task prototype, not production-ready.

    Bodies rerun from the start on another serving replica after node loss
    or lease expiry. Leases are not renewed; longer bodies may run twice.
    Results are discarded. Bodies must be idempotent or use application
    idempotency keys; submit keys deduplicate only until acknowledgement.
    There is no dead-letter queue or attempt cap. Replicated Mnesia without
    majority is not partition-safe. Only same-host process crashes are tested.
    """

    def __init__(self, name: str, *, max_items: int = 10_000, replicas: Sequence[str] = ()) -> None:
        self._queue = DurableQueue(name, max_items=max_items, replicas=replicas)

    async def submit(
        self,
        function: Callable[[bytes], Awaitable[bytes]],
        payload: bytes,
        *,
        key: str | None = None,
    ) -> bool:
        """Sync intent before returning True; False means a retained duplicate key.

        A plain importable top-level async byte function is required. Resolution
        uses its module and name in the native runtime, not a transferred callable.
        The 1 MiB limit includes the stored module/function/payload record.
        Cancellation is uncertain: the task may be stored. Retry with the same
        key while it is retained; keys are removed on acknowledgement.
        """
        if not isinstance(function, FunctionType) or not iscoroutinefunction(function):
            raise TypeError("Durable tasks require a plain async Python function")
        if (
            not isinstance(function.__module__, str)
            or not function.__qualname__.isidentifier()
            or not all(part.isidentifier() for part in function.__module__.split("."))
        ):
            raise TypeError("Durable tasks require importable top-level functions")
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if len(payload) > 1_048_576:
            raise PayloadTooLarge("Stored task record exceeds 1 MiB")
        if key is not None and not isinstance(key, str):
            raise TypeError("key must be text or None")
        await self._queue._open()
        result = await _call(
            "_tasks_submit",
            self._queue._name,
            None if key is None else key.encode(),
            function.__module__.encode(),
            function.__qualname__.encode(),
            payload,
        )
        return _stored(result)

    @asynccontextmanager
    async def serve(self, *, concurrency: int = 4, lease: float = 60.0) -> AsyncGenerator[None]:
        """Own this node's BEAM workers until exit, including error or cancellation.

        Each worker owns a Python execution context. Exit stops the workers and
        releases in-flight claims when possible; otherwise leases expire.
        Enter cancellation waits for startup to settle, then stops owned workers.
        """
        if type(concurrency) is not int or not 1 <= concurrency <= 1024:
            raise ValueError("concurrency must be an integer from 1 to 1024")
        lease_ms = _lease_ms(lease)
        await self._queue._open()
        pending = asyncio.create_task(
            _call("_tasks_serve", self._queue._name, concurrency, lease_ms)
        )
        try:
            result = await asyncio.shield(pending)
        except asyncio.CancelledError:
            result = await _finish(pending)
            if result == "ok":
                await _settle(asyncio.create_task(_call("_tasks_stop", self._queue._name)))
            raise
        if result != "ok":
            raise QueueStoreError("Invalid serve response")
        try:
            yield
        finally:
            await _settle(asyncio.create_task(_call("_tasks_stop", self._queue._name)))
