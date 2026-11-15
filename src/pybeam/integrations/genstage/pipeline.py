import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import FunctionType

from pybeam.runtime.optional import start_optional


class CallbackFailed(RuntimeError):
    pass


@asynccontextmanager
async def stream(
    count: int,
    callback: Callable[[bytes], bytes],
    *,
    action: str = "ok",
    max_bytes: int | None = None,
) -> AsyncGenerator[AsyncIterator[bytes]]:
    """Own a finite, single-credit GenStage graph and await context drain on exit.

    The first stream starts the bundled optional OTP applications in the current
    managed VM. Core imports and other features do not start these applications.
    At most one request is admitted, including running Python work. Requests
    and replies are limited to 1024 encoded bytes each, after serialization.
    This is not a heap, temporary-allocation or arbitrary-mailbox bound.
    A total byte bound is unsupported and fails closed. No retries occur.
    Cancellation can leave an effect completed without its reply being observed.
    """
    if max_bytes is not None:
        raise NotImplementedError("A hard graph memory bound is not available")
    if type(count) is not int or not 1 <= count <= 16:
        raise ValueError("count must be between 1 and 16")
    if not isinstance(callback, FunctionType):
        raise TypeError("callback must be a Python function")
    if (
        callback.__module__ == "__main__"
        or "." in callback.__qualname__
        or not callback.__name__.isidentifier()
    ):
        raise ValueError("callback must be an importable module-level function")
    await start_optional("genstage")
    import erlang

    opening = asyncio.create_task(
        erlang.async_call(
            "pybeam_genstage", b"open", count, callback.__module__, callback.__name__, action
        )
    )

    async def close(graph: object) -> None:
        closing = asyncio.create_task(erlang.async_call("pybeam_genstage", b"close", graph))
        try:
            result = await asyncio.shield(closing)
        except asyncio.CancelledError:
            result = await closing
            if result != "ok":
                raise CallbackFailed(result) from None
            raise
        if result != "ok":
            raise CallbackFailed(result)

    try:
        graph = await asyncio.shield(opening)
    except asyncio.CancelledError:
        graph = await opening
        await close(graph)
        raise

    async def events() -> AsyncIterator[bytes]:
        for _ in range(count):
            result = await erlang.async_call("pybeam_genstage", b"next", graph)
            if not isinstance(result, tuple) or len(result) != 2:
                raise CallbackFailed(result)
            status, payload = result
            if status != "ok" or not isinstance(payload, (bytes, str)):
                raise CallbackFailed(result)
            yield payload.encode("utf-8") if isinstance(payload, str) else payload

    try:
        yield events()
    finally:
        await close(graph)
