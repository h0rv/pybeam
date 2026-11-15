import asyncio
import os
import tempfile
from contextlib import ExitStack
from dataclasses import field
from io import BufferedRandom
from weakref import ReferenceType, ref

from pydantic import BaseModel, ConfigDict

import pybeam
from pybeam.actors.classes import Reference, reference_route


@pybeam.actor
class Counter:
    value: int = 0

    async def add(self, n: int, *, twice: bool = False) -> int:
        await asyncio.sleep(0)
        self.value += n * (2 if twice else 1)
        return self.value

    async def inline(self, n: int) -> int:
        return await self.add(n)

    async def read(self) -> int:
        return self.value

    async def large(self) -> str:
        return "x" * 1_048_576

    async def wrong_return(self) -> int:
        return True

    async def echo(self, value: bytes) -> bytes:
        return value


class Payload(BaseModel):
    model_config = ConfigDict(frozen=True)
    values: list[int]


@pybeam.actor
class Store:
    values: list[int] = field(default_factory=list)

    async def put(self, payload: Payload) -> list[int]:
        self.values = payload.values
        return self.values

    async def read(self) -> list[int]:
        return self.values


DEFAULT = Payload(values=[])


@pybeam.actor
class Defaults:
    payload: Payload = DEFAULT
    private: Payload = field(init=False, default=DEFAULT)

    def __post_init__(self) -> None:
        self.private.values.append(4)

    async def add(self, value: int) -> tuple[list[int], list[int]]:
        self.payload.values.append(value)
        return self.payload.values, self.private.values


CONSTRUCTIONS: list[int] = []
FILES: list[ReferenceType[BufferedRandom]] = []


@pybeam.actor
class Resource:
    initial: int = 0
    _file: BufferedRandom = field(init=False, repr=False)
    _resources: ExitStack = field(init=False, repr=False)

    def __post_init__(self) -> None:
        CONSTRUCTIONS.append(self.initial)
        if self.initial < 0:
            raise ValueError("constructor failed")
        with ExitStack() as resources:
            self._file = resources.enter_context(tempfile.TemporaryFile())
            self._resources = resources.pop_all()
        FILES.append(ref(self._file))

    async def descriptor(self) -> int:
        return self._file.fileno()


async def verify() -> None:
    from erlang.actors import ActorOverloaded, DataActorRef

    async with pybeam.ActorGroup() as actors:
        counter = await actors.spawn(Counter, value=0)
        assert await counter.inline(1) == 1
        assert await counter.add(2, twice=True) == 5
        values = await asyncio.gather(*(counter.add(1) for _ in range(40)))
        assert sorted(values) == list(range(6, 46))
        try:
            _ = counter.value
        except pybeam.ActorStateAccessError:
            pass
        else:
            raise AssertionError("Actor reference exposed local state")
        try:
            await counter.large()
        except pybeam.ActorPayloadError:
            pass
        else:
            raise AssertionError("Encoded reply size was unbounded")
        try:
            await counter.wrong_return()
        except pybeam.ActorPayloadError:
            pass
        else:
            raise AssertionError("Strict return validation was bypassed")
        try:
            await counter.add(True)
        except pybeam.ActorPayloadError:
            pass
        else:
            raise AssertionError("Strict argument validation was bypassed")
        assert await counter.echo(b"\x00\xff") == b"\x00\xff"
        try:
            await counter.echo(b"x" * 800_000)
        except ActorOverloaded:
            pass
        else:
            raise AssertionError("Encoded request byte limit was bypassed")
        assert await counter.read() == 45
        assert isinstance(counter, Reference)
        native = reference_route(counter).native
        assert isinstance(native, DataActorRef)
        tickets = [native.reserve(b"0") for _ in range(64)]
        try:
            try:
                native.reserve(b"0")
            except RuntimeError:
                pass
            else:
                raise AssertionError("Default mailbox count was unbounded")
            try:
                await counter.read()
            except ActorOverloaded:
                pass
            else:
                raise AssertionError("Public actor call bypassed native count admission")
        finally:
            for ticket in tickets:
                ticket.abort()
        try:
            native.reserve(b"0" * 1_048_577)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Default encoded byte admission was unbounded")
        left = await actors.spawn(Defaults)
        right = await actors.spawn(Defaults)
        assert await left.add(1) == ([1], [4])
        assert await right.add(2) == ([2], [4])
        assert DEFAULT.values == []
        store = await actors.spawn(Store)
        payload = Payload(values=[1])
        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(store.put(payload))
            await asyncio.sleep(0)
            payload.values.append(2)
        returned = pending.result()
        assert returned == [1]
        returned.append(3)
        assert await store.read() == [1]
        await actors.stop(counter)
        try:
            await Counter.read(counter)
        except pybeam.ActorStoppedError:
            pass
        else:
            raise AssertionError("Stopped unbound method executed locally")
    try:
        await store.read()
    except pybeam.ActorStoppedError:
        pass
    else:
        raise AssertionError("Closed group reference was usable")
    async with pybeam.ActorGroup(max_children=1) as actors:
        resource = await actors.spawn(Resource, initial=3)
        assert CONSTRUCTIONS == [3]
        descriptor = await resource.descriptor()
        try:
            await actors.spawn(Counter)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Native group child count was unbounded")
        assert await resource.descriptor() == descriptor
    assert FILES[-1]() is None, "Native context retained its private resource"
    try:
        os.fstat(descriptor)
    except OSError:
        pass
    else:
        raise AssertionError("Private file descriptor survived native group shutdown")
    async with pybeam.ActorGroup() as actors:
        try:
            await actors.spawn(Resource, initial=-1)
        except pybeam.ActorConstructionError:
            pass
        else:
            raise AssertionError("Constructor failure was not returned to spawn")
        assert CONSTRUCTIONS == [3, -1]
    async with pybeam.spawn(Counter, value=7) as single:
        assert await single.add(2) == 9
    for _ in range(5):
        group = await pybeam.ActorGroup().__aenter__()
        async with asyncio.TaskGroup() as tasks:
            starting = tasks.create_task(group.spawn(Counter))
            await asyncio.sleep(0)
            closing = tasks.create_task(group.__aexit__(None, None, None))
        assert closing.done()
        try:
            await starting.result().read()
        except pybeam.ActorStoppedError:
            pass
        else:
            raise AssertionError("A racing start escaped group shutdown")
    async with pybeam.ActorGroup() as actors, asyncio.TaskGroup() as tasks:
        pending = tasks.create_task(actors.spawn(Resource, initial=99))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancelled startup returned a reference")
    assert all(resource() is None for resource in FILES)
    print("pythonic-native-ok", flush=True)


async def restart() -> bool:
    from erlang import async_call
    from erlang.actors import ActorGenerationChanged, DataActorRef

    async with pybeam.ActorGroup() as actors:
        counter = await actors.spawn(Counter, value=5)
        assert await counter.add(2) == 7
        assert isinstance(counter, Reference)
        native = reference_route(counter).native
        assert isinstance(native, DataActorRef)
        await async_call("pythonic_kill", native.pid)
        try:
            await counter.add(100)
        except ActorGenerationChanged:
            pass
        else:
            raise AssertionError("Old generation was implicitly replayed")
        fresh = await actors.rejoin(counter)
        assert await fresh.read() == 5
        assert await fresh.add(1) == 6
    return True


if __name__ == "__main__":
    pybeam.run(verify)
