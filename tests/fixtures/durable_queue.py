import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path

import pybeam
from pybeam.queues import DurableQueue, LeaseExpired
from pybeam.runtime.ownership import durable_write


async def proof() -> None:
    queue = DurableQueue("proof", max_items=2)
    assert await queue.put(b"value", key="key")
    assert not await queue.put(b"other", key="key")
    async with queue.get() as item:
        assert item.payload == b"value" and item.attempt == 1
    try:
        assert await queue.put(b"retry")
        async with queue.get(lease=0.02):
            await asyncio.sleep(0.05)
    except LeaseExpired:
        pass
    else:
        raise AssertionError("Expired ack succeeded")
    async with queue.get() as item:
        assert item.payload == b"retry" and item.attempt == 2

    async def receive() -> None:
        async with queue.get():
            raise AssertionError("Cancelled waiter delivered")

    pending = asyncio.create_task(receive())
    await asyncio.sleep(0.03)
    pending.cancel()
    assert await queue.put(b"cancel")
    with suppress(asyncio.CancelledError):
        await pending
    async with queue.get() as item:
        assert item.payload == b"cancel" and item.attempt == 2
    assert await queue.put(b"release")
    try:
        async with queue.get():
            raise LookupError("retry")
    except LookupError:
        pass
    async with queue.get() as item:
        assert item.attempt == 2
    print("QUEUE_PROOF ack lease_retry release cancellation", flush=True)


async def main() -> None:
    queue = DurableQueue("crash")
    if sys.argv[1] == "proof":
        await proof()
    elif sys.argv[1] == "put":
        assert await queue.put(b"persisted", key="crash-key")
        await asyncio.to_thread(durable_write, Path("stored.pid"), str(os.getpid()))
        await asyncio.Event().wait()
    else:
        async with queue.get() as item:
            assert item.payload == b"persisted" and item.attempt == 1
        assert await queue.put(b"key released", key="crash-key")
        async with queue.get() as item:
            assert item.payload == b"key released"
        print("QUEUE_RECOVERED acknowledged_put", flush=True)


pybeam.run(main)
