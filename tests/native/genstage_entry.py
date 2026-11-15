import asyncio

from genstage_callbacks import Outcome, process
from genstage_owned import raises

import pybeam
from pybeam.integrations.genstage.pipeline import CallbackFailed, stream
from pybeam.runtime.optional import start_optional


async def contexts() -> int:
    import erlang

    value = await erlang.async_call("pybeam_genstage", b"contexts")
    assert isinstance(value, int)
    return value


async def main() -> None:
    await start_optional("genstage")
    baseline = await contexts()
    for _ in range(8):
        async with stream(16, process) as events:
            async for payload in events:
                assert Outcome.model_validate_json(payload).id == 0
                break
        assert await contexts() == baseline

    ready = asyncio.Event()

    async def consume() -> None:
        async with stream(16, process, action="slow") as events:
            ready.set()
            async for _ in events:
                pass

    for _ in range(8):
        ready.clear()
        task = asyncio.create_task(consume())
        await ready.wait()
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancellation was not propagated")
        assert await contexts() == baseline

    for _ in range(8):
        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Acquisition cancellation was not propagated")
        assert await contexts() == baseline

    try:
        async with stream(16, raises) as events:
            await anext(events)
    except CallbackFailed as error:
        assert "RuntimeError" in str(error)
    else:
        raise AssertionError("Callback failure was hidden")
    assert await contexts() == baseline
    for hard_limit in (1, 1024):
        try:
            async with stream(16, process, max_bytes=hard_limit):
                raise AssertionError("Unsupported hard bound was accepted")
        except NotImplementedError:
            pass
    print("Python break/cancel/failure churn drained native contexts to baseline", baseline)


pybeam.run(main)
