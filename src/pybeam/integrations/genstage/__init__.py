import asyncio
import os
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import FunctionType


def enable() -> None:
    """Opt into private GenStage assets before pybeam.run starts its owned VM.

    Fresh wheels do not yet bundle this optional artifact. The development
    override must point at a privately staged runtime dependency closure.
    """
    root = Path(os.environ.get("PYBEAM_GENSTAGE_ASSETS", Path(__file__).with_name("_assets")))
    paths = tuple(sorted(root.glob("*/ebin")))
    required = ("elixir.app", "gen_stage.app", "pybeam_genstage.beam")
    if not all(any((path / name).is_file() for path in paths) for name in required):
        raise RuntimeError(
            "Optional GenStage runtime artifact is unavailable. This wheel does not bundle it; "
            "developers must stage the pinned private assets and set PYBEAM_GENSTAGE_ASSETS."
        )
    if any(any(character.isspace() for character in str(path)) for path in paths):
        raise RuntimeError("Optional GenStage development asset paths cannot contain whitespace")
    flags = "-pa " + " ".join(str(path.resolve()) for path in paths)
    flags += " -s pybeam_genstage bootstrap"
    if flags not in os.environ.get("ERL_AFLAGS", ""):
        os.environ["ERL_AFLAGS"] = flags + " " + os.environ.get("ERL_AFLAGS", "")


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
