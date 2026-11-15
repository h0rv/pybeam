import asyncio
import gc
from contextlib import suppress
from dataclasses import dataclass
from typing import ClassVar
from weakref import ReferenceType, ref

from pybeam import run
from pybeam.actors import ActorSpec, MethodError, UnboundedMailbox, spawn


@dataclass(slots=True, weakref_slot=True)
class Payload:
    pass


@dataclass(slots=True, weakref_slot=True)
class Counter:
    instances: ClassVar[list[ReferenceType[Counter]]] = []
    value: int = 0

    def __post_init__(self) -> None:
        self.instances.append(ref(self))

    async def add(self, amount: int, /, *, label: object) -> tuple[int, object]:
        old = self.value
        await asyncio.sleep(0.001)
        self.value = old + amount
        return self.value, label

    async def read(self) -> int:
        return self.value

    async def echo[T](self, value: T) -> T:
        return value

    async def fail(self, error: BaseException) -> None:
        raise error

    async def hold(self, started: asyncio.Event, release: asyncio.Event, value: object) -> object:
        started.set()
        await release.wait()
        self.value += 1
        return value

    async def forbidden(self) -> None:
        raise AssertionError("Unregistered method executed")


FACTORY_ERROR = ValueError("factory")


class Broken:
    def __init__(self) -> None:
        raise FACTORY_ERROR

    async def read(self) -> int:
        return 0


async def check() -> None:
    try:
        await spawn(ActorSpec(Broken, methods=(Broken.read,)), mailbox=UnboundedMailbox())
    except ValueError as caught:
        assert caught is FACTORY_ERROR
    else:
        raise AssertionError("Factory exception identity was lost")
    FACTORY_ERROR.__traceback__ = None
    spec = ActorSpec(
        Counter,
        methods=(Counter.add, Counter.read, Counter.echo, Counter.fail, Counter.hold),
    )
    async with asyncio.TaskGroup() as tasks:
        starting = tasks.create_task(spawn(spec, mailbox=UnboundedMailbox()))
        await asyncio.sleep(0)
        starting.cancel()
    assert starting.cancelled()
    payload = object()
    first = await spawn(spec, mailbox=UnboundedMailbox())
    second = await spawn(spec, mailbox=UnboundedMailbox())
    async with first, second:
        async with asyncio.TaskGroup() as tasks:
            increments = [
                tasks.create_task(first.call(Counter.add, 1, label=payload)) for _ in range(12)
            ]
            other = tasks.create_task(second.call(Counter.add, 3, label=payload))
        assert sorted(task.result()[0] for task in increments) == list(range(1, 13))
        assert all(task.result()[1] is payload for task in increments)
        assert other.result() == (3, payload)
        assert await first.call(Counter.echo, payload) is payload
        assert await first.call(Counter.read) == 12
        assert await second.call(Counter.read) == 3
        error = ValueError(payload)
        try:
            await first.call(Counter.fail, error)
        except ValueError as caught:
            assert caught is error
            assert caught.args[0] is payload
        else:
            raise AssertionError("Original exception was lost")
        error.__traceback__ = None
        cancellation = asyncio.CancelledError(payload)
        try:
            await first.call(Counter.fail, cancellation)
        except asyncio.CancelledError as caught_cancellation:
            assert caught_cancellation is cancellation
        else:
            raise AssertionError("Actor exception became a different cancellation outcome")
        cancellation.__traceback__ = None
        try:
            await first.call(Counter.forbidden)
        except MethodError:
            pass
        else:
            raise AssertionError("Membership guard was bypassed")

        started, release = asyncio.Event(), asyncio.Event()
        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(first.call(Counter.hold, started, release, payload))
            await started.wait()
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
            queued = tasks.create_task(first.call(Counter.read))
            await asyncio.sleep(0.01)
            assert not queued.done(), "Cancellation released actor serialization early"
            release.set()
        assert queued.result() == 13

        started, release = asyncio.Event(), asyncio.Event()
        timed_payload = Payload()
        timed_reference = ref(timed_payload)
        try:
            async with asyncio.timeout(0.01):
                await first.call(Counter.hold, started, release, timed_payload)
        except TimeoutError:
            assert started.is_set()
        else:
            raise AssertionError("Caller timeout was lost")
        del timed_payload
        gc.collect()
        assert timed_reference() is not None, "Timeout freed an in-flight argument"
        async with asyncio.TaskGroup() as tasks:
            queued = tasks.create_task(first.call(Counter.read))
            await asyncio.sleep(0.01)
            assert not queued.done(), "Timeout released actor serialization early"
            release.set()
        assert queued.result() == 14

        started, release = asyncio.Event(), asyncio.Event()
        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(second.call(Counter.hold, started, release, payload))
            await started.wait()
            stopping = tasks.create_task(second.stop())
            await asyncio.sleep(0.01)
            assert not stopping.done(), "Stop returned while owned work was running"
            stopping.cancel()
            release.set()
        assert pending.result() is payload
        assert stopping.cancelled()
        await second.stop()

    await first.stop()
    try:
        await first.call(Counter.read)
    except RuntimeError:
        pass
    else:
        raise AssertionError("Stopped actor accepted work")
    del increments, other, pending, queued, error, cancellation
    await asyncio.sleep(0)
    gc.collect()
    assert all(instance() is None for instance in Counter.instances), "Actor state leaked"
    assert timed_reference() is None, "Stopped native work retained its argument"
    print("PUBLIC_ACTORS_OK", flush=True)


async def main() -> None:
    async with asyncio.timeout(15):
        await check()


if __name__ == "__main__":
    run(main)
