import asyncio
import math
import socket
from contextlib import suppress

from erlang import Channel

from pybeam.services.background import ServiceBusyError, ServiceSlot


class Resource:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.release_allowed = asyncio.Event()
        self.ticks = 0


async def consume(resource: Resource, *, pause: float) -> None:
    resource.started.set()
    try:
        while True:
            resource.ticks += 1
            await asyncio.sleep(pause)
    finally:
        resource.released.set()


async def fail() -> None:
    raise LookupError("service failure")


async def fail_cleanup(resource: Resource) -> None:
    resource.started.set()
    try:
        await asyncio.Event().wait()
    finally:
        resource.released.set()
        raise ValueError("cleanup failure")


async def slow_cleanup(resource: Resource) -> None:
    resource.started.set()
    try:
        await asyncio.Event().wait()
    finally:
        await resource.release_allowed.wait()
        resource.released.set()


async def finish(value: int) -> None:
    assert value == 42


async def read_socket(reader: socket.socket, started: asyncio.Event) -> None:
    try:
        started.set()
        await asyncio.get_running_loop().sock_recv(reader, 1)
    finally:
        reader.close()


async def receive_channel(channel: Channel, started: asyncio.Event) -> None:
    try:
        started.set()
        await channel.async_receive(native_only=True)
    finally:
        channel.close()


async def verify_channel(channel_ref: object) -> None:
    slot = ServiceSlot(capacity=1)
    started = asyncio.Event()
    ref = slot.start(receive_channel, Channel(channel_ref), started)
    await started.wait()
    await asyncio.sleep(0)
    await ref.stop(timeout=0.5)
    assert ref.state == "cancelled"


async def verify() -> str:
    slot = ServiceSlot(capacity=1)
    resource = Resource()
    ref = slot.start(consume, resource, pause=0.001)
    await resource.started.wait()
    try:
        await ref.wait(timeout=0.01)
    except TimeoutError:
        pass
    else:
        raise AssertionError("wait must time out without cancelling")
    assert ref.state == "running"
    before = resource.ticks
    await asyncio.sleep(0.01)
    assert resource.ticks > before
    assert not resource.released.is_set()
    try:
        slot.start(fail)
    except ServiceBusyError:
        pass
    else:
        raise AssertionError("a slot must reject excess admission")

    waiter = asyncio.create_task(ref.wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with suppress(asyncio.CancelledError):
        await waiter
    assert ref.state == "running"
    try:
        slot.start(fail)
    except ServiceBusyError:
        pass
    else:
        raise AssertionError("abandoning a waiter must not free admission")
    for invalid in (-1.0, math.inf, math.nan):
        try:
            await ref.stop(timeout=invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("stop must reject unbounded deadlines")
    assert ref.state == "running"
    await ref.stop(timeout=0.5)
    assert resource.released.is_set()
    assert ref.state == "cancelled"
    await ref.stop(timeout=0)
    try:
        await ref.wait()
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("wait must expose cancellation")

    failed = slot.start(fail)
    for _ in range(2):
        try:
            await failed.wait(timeout=0.5)
        except LookupError as error:
            assert str(error) == "service failure"
        else:
            raise AssertionError("failure must remain observable")
    assert failed.state == "failed"
    try:
        await failed.stop(timeout=0.5)
    except LookupError:
        pass
    else:
        raise AssertionError("stop must expose prior task failure")

    resource = Resource()
    failed = slot.start(fail_cleanup, resource)
    await resource.started.wait()
    try:
        await failed.stop(timeout=0.5)
    except ValueError as error:
        assert str(error) == "cleanup failure"
    else:
        raise AssertionError("stop must not acknowledge failed cleanup")
    assert resource.released.is_set()
    assert failed.state == "failed"

    resource = Resource()
    slow = slot.start(slow_cleanup, resource)
    await resource.started.wait()
    try:
        await slow.stop(timeout=0.01)
    except TimeoutError:
        pass
    else:
        raise AssertionError("stop must bound the wait for cleanup")
    assert slow.state == "running"
    assert not resource.released.is_set()
    try:
        slot.start(fail)
    except ServiceBusyError:
        pass
    else:
        raise AssertionError("stop timeout must not free admission")
    cancelled_stop = asyncio.create_task(slow.stop(timeout=0.5))
    await asyncio.sleep(0)
    cancelled_stop.cancel()
    with suppress(asyncio.CancelledError):
        await cancelled_stop
    assert not resource.released.is_set()
    assert slow.state == "running"
    stopping = asyncio.create_task(slow.stop(timeout=0.5))
    await asyncio.sleep(0)
    resource.release_allowed.set()
    await stopping
    assert resource.released.is_set()
    assert slow.state == "cancelled"

    done = slot.start(finish, 42)
    await done.wait(timeout=0.5)
    await done.wait(timeout=0)
    assert done.state == "completed"

    async def closure() -> None:
        return None

    try:
        slot.start(closure)
    except ValueError:
        pass
    else:
        raise AssertionError("closures must be rejected")
    try:
        slot.start(lambda: finish(42))
    except TypeError:
        pass
    else:
        raise AssertionError("lambdas must be rejected")
    reader, writer = socket.socketpair()
    reader.setblocking(False)
    started = asyncio.Event()
    try:
        reading = slot.start(read_socket, reader, started)
        await started.wait()
        await asyncio.sleep(0)
        await reading.stop(timeout=0.5)
        assert reader.fileno() == -1
        assert reading.state == "cancelled"
    finally:
        reader.close()
        writer.close()
    await slot.stop(timeout=0.5)
    pending = asyncio.all_tasks() - {asyncio.current_task()}
    assert not pending, repr(pending)
    return "services-ok"
