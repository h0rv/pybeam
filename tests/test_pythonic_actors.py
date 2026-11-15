import asyncio
import itertools
import sys
from collections.abc import Callable
from dataclasses import field
from types import ModuleType

import pytest
from pydantic import BaseModel, ConfigDict

import pybeam
from pybeam.actors import groups
from pybeam.actors.classes import Container, DataActor, definition
from pybeam.actors.codec import ActorPayloadError


@pybeam.actor
class Counter:
    value: int = 0

    async def add(self, n: int, *, double: bool = False) -> int:
        self.value += n * (2 if double else 1)
        return self.value

    async def inline(self, n: int) -> int:
        return await self.add(n)

    async def constant(self) -> int:
        return 42


class ScriptedActor:
    def __init__(self, state: Container, child_id: str) -> None:
        self.state = state
        self.child_id = child_id
        self.closed = False
        self.admitted = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def rejoin(self) -> ScriptedActor:
        return self

    async def call(self, payload: bytes, /) -> bytes:
        if self.closed:
            raise RuntimeError("stopped")
        self.admitted.set()
        await self.release.wait()
        return await self.state.dispatch(payload)


class ScriptedGroup:
    def __init__(self) -> None:
        self.actors: list[ScriptedActor] = []
        self.closed = False
        self.admitted = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def spawn(
        self,
        child_id: str,
        factory: Callable[[], Container],
        *,
        capacity: int = 64,
        max_bytes: int = 1_048_576,
    ) -> DataActor:
        assert child_id not in {str(i) for i in range(len(self.actors))}
        assert (capacity, max_bytes) == (64, 1_048_576)
        self.admitted.set()
        await self.release.wait()
        actor = ScriptedActor(factory(), child_id)
        self.actors.append(actor)
        return actor

    async def stop(self, child_id: str, /) -> None:
        for actor in self.actors:
            if actor.child_id == child_id:
                actor.closed = True

    async def close(self) -> None:
        self.closed = True
        for actor in self.actors:
            actor.closed = True


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> ScriptedGroup:
    backend = ScriptedGroup()

    async def open_group(*, max_children: int) -> ScriptedGroup:
        assert max_children == 128
        return backend

    monkeypatch.setattr(groups, "open_group", open_group)
    return backend


async def test_reference__routes_direct_and_unbound_methods_and_blocks_state(
    backend: ScriptedGroup,
) -> None:
    async with pybeam.ActorGroup() as actors:
        counter = await actors.spawn(Counter, value=0)
        assert isinstance(counter, Counter)
        assert await counter.add(2, double=True) == 4
        assert await counter.inline(3) == 7
        assert await Counter.add(counter, 1) == 8
        with pytest.raises(pybeam.ActorStateAccessError, match="state"):
            _ = counter.value
        with pytest.raises(pybeam.ActorStateAccessError, match="state"):
            counter.value = 90
        with pytest.raises(pybeam.ActorStateAccessError):
            _ = counter.__dict__
    with pytest.raises(pybeam.ActorStoppedError):
        await counter.constant()
    with pytest.raises(pybeam.ActorStoppedError):
        await Counter.constant(counter)
    assert backend.closed


async def test_spawn__accepts_concurrent_child_tasks(backend: ScriptedGroup) -> None:
    async with pybeam.ActorGroup() as actors:
        counters = await asyncio.gather(*map(actors.spawn, itertools.repeat(Counter, 5)))
        assert await asyncio.gather(*(counter.add(1) for counter in counters)) == [1] * 5
    assert len(backend.actors) == 5
    assert all(actor.closed for actor in backend.actors)


async def test_membership__rejects_other_groups_and_stopped_refs(backend: ScriptedGroup) -> None:
    async with pybeam.ActorGroup() as first, pybeam.ActorGroup() as second:
        counter = await first.spawn(Counter)
        with pytest.raises(ValueError, match="another group"):
            await second.stop(counter)
        fresh = await first.rejoin(counter)
        assert isinstance(fresh, Counter)
        with pytest.raises(pybeam.ActorStoppedError):
            await counter.constant()
        assert await fresh.add(2) == 2
        await first.stop(fresh)
        with pytest.raises(pybeam.ActorStoppedError):
            await fresh.constant()
        assert backend.actors[0].closed


async def test_single_context__uses_group_ownership(backend: ScriptedGroup) -> None:
    async with pybeam.spawn(Counter, value=10) as counter:
        assert await counter.add(1) == 11
    assert backend.closed


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

    async def echo_bytes(self, value: bytes) -> bytes:
        return value


async def test_boundary__copies_inflight_arguments_and_results(backend: ScriptedGroup) -> None:
    async with pybeam.ActorGroup() as actors:
        store = await actors.spawn(Store)
        native = backend.actors[0]
        native.admitted.clear()
        native.release.clear()
        payload = Payload(values=[1])
        async with asyncio.TaskGroup() as tasks:
            pending = tasks.create_task(store.put(payload))
            await native.admitted.wait()
            payload.values.append(2)
            native.release.set()
        reply = pending.result()
        assert reply == [1]
        reply.append(3)
        assert await store.read() == [1]
        assert await store.echo_bytes(b"\x00\xff") == b"\x00\xff"


async def test_defaults__copy_shallow_frozen_models_between_actors(backend: ScriptedGroup) -> None:
    default = Payload(values=[])

    @pybeam.actor
    class NestedDefaults:
        payload: Payload = default
        private: Payload = field(init=False, default=default)

        def __post_init__(self) -> None:
            self.private.values.append(5)

        async def append(self, value: int) -> tuple[list[int], list[int]]:
            self.payload.values.append(value)
            return self.payload.values, self.private.values

    async with pybeam.ActorGroup() as actors:
        first = await actors.spawn(NestedDefaults)
        second = await actors.spawn(NestedDefaults)
        assert await first.append(1) == ([1], [5])
        assert await second.append(2) == ([2], [5])
        assert default.values == []


async def test_boundary__supports_private_and_model_named_parameters(
    backend: ScriptedGroup,
) -> None:
    @pybeam.actor
    class Parameters:
        _prefix: str = ""

        async def check(self, model_dump: int, /, *, _value: int = 2) -> str:
            return self._prefix * (model_dump + _value)

    async with pybeam.ActorGroup() as actors:
        reference = await actors.spawn(Parameters, _prefix="a")
        assert await reference.check(1, _value=3) == "aaaa"
        with pytest.raises(pybeam.ActorStateAccessError):
            _ = reference._prefix


def test_boundary__rejects_unsupported_objects_instead_of_sharing_them() -> None:
    @pybeam.actor
    class Objects:
        async def receive(self, value: object) -> None:
            pass

    method = definition(Objects).methods[0]
    with pytest.raises(ActorPayloadError, match="serialize"):
        method.arguments.encode((object(),), {})


async def test_boundary__rejects_wrong_arguments_before_admission(backend: ScriptedGroup) -> None:
    async with pybeam.ActorGroup() as actors:
        counter = await actors.spawn(Counter)
        method = definition(Counter).methods[0]
        backend.actors[0].admitted.clear()
        with pytest.raises(ActorPayloadError):
            method.arguments.encode(("not an int",), {})
        assert not backend.actors[0].admitted.is_set()
        with pytest.raises(ActorPayloadError):
            definition(Counter).constructor.encode((), {"value": "wrong"})
        assert await counter.add(1) == 1


async def test_constructor__runs_once_and_failure_does_not_close_group(
    backend: ScriptedGroup,
) -> None:
    constructions: list[int] = []

    @pybeam.actor
    class Constructed:
        value: int

        def __post_init__(self) -> None:
            constructions.append(self.value)
            if self.value < 0:
                raise ValueError("constructor failed")

        async def read(self) -> int:
            return self.value

    async with pybeam.ActorGroup() as actors:
        reference = await actors.spawn(Constructed, value=1)
        assert constructions == [1]
        assert await reference.read() == 1
        with pytest.raises(pybeam.ActorConstructionError, match="constructor failed"):
            await actors.spawn(Constructed, value=-1)
        assert await reference.read() == 1
    assert constructions == [1, -1]


async def test_close__owns_admitted_start_and_rejects_late_spawn(backend: ScriptedGroup) -> None:
    backend.release.clear()
    group = pybeam.ActorGroup()
    await group.__aenter__()
    async with asyncio.TaskGroup() as tasks:
        start = tasks.create_task(group.spawn(Counter))
        await backend.admitted.wait()
        close = tasks.create_task(group.__aexit__(None, None, None))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="not open"):
            await group.spawn(Counter)
        assert not close.done()
        backend.release.set()
    with pytest.raises(pybeam.ActorStoppedError):
        await start.result().constant()
    assert backend.closed


async def test_close_cancellation__still_joins_admitted_starts(backend: ScriptedGroup) -> None:
    backend.release.clear()
    group = await pybeam.ActorGroup().__aenter__()
    async with asyncio.TaskGroup() as tasks:
        start = tasks.create_task(group.spawn(Counter))
        await backend.admitted.wait()
        close = tasks.create_task(group.__aexit__(None, None, None))
        await asyncio.sleep(0)
        close.cancel()
        second_close = tasks.create_task(group.__aexit__(None, None, None))
        await asyncio.sleep(0)
        assert not close.done()
        assert not second_close.done()
        backend.release.set()
        with pytest.raises(asyncio.CancelledError):
            await close
        await second_close
    assert backend.closed
    with pytest.raises(pybeam.ActorStoppedError):
        await start.result().constant()


async def test_cancelled_spawn__leaves_no_unowned_child(backend: ScriptedGroup) -> None:
    backend.release.clear()
    async with pybeam.ActorGroup() as group, asyncio.TaskGroup() as tasks:
        start = tasks.create_task(group.spawn(Counter))
        await backend.admitted.wait()
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start
    assert backend.closed
    assert not backend.actors


async def test_errors__do_not_transfer_original_exception_or_state(backend: ScriptedGroup) -> None:
    errors: list[Exception] = []

    class StateError(Exception):
        pass

    @pybeam.actor
    class Failing:
        value: int = 1

        async def fail(self) -> None:
            error = StateError(self)
            errors.append(error)
            raise error

        async def read(self) -> int:
            return self.value

    async with pybeam.ActorGroup() as actors:
        reference = await actors.spawn(Failing)
        with pytest.raises(pybeam.ActorCallError) as raised:
            await reference.fail()
        assert raised.value.kind == "StateError"
        assert raised.value is not errors[0]
        assert all(isinstance(argument, str) for argument in raised.value.args)
        assert raised.value.__context__ is None
        assert await reference.read() == 1


async def test_actor_state__does_not_grant_ownership_to_child_tasks(backend: ScriptedGroup) -> None:
    @pybeam.actor
    class TaskOwner:
        value: int = 0

        async def constant(self) -> int:
            return 1

        async def check(self) -> bool:
            bound = self.constant

            async def child() -> bool:
                try:
                    await bound()
                except pybeam.ActorStateAccessError:
                    return True
                return False

            async with asyncio.TaskGroup() as tasks:
                checked = tasks.create_task(child())
            self.value += 1
            return checked.result()

    async with pybeam.ActorGroup() as actors:
        reference = await actors.spawn(TaskOwner)
        assert await reference.check()


async def test_inheritance__preserves_constructor_and_inline_calls(backend: ScriptedGroup) -> None:
    @pybeam.actor
    class Derived(Counter):
        step: int = 2

        async def advance(self) -> int:
            return await self.add(self.step)

    async with pybeam.ActorGroup() as actors:
        counter = await actors.spawn(Derived, value=4, step=3)
        assert await counter.advance() == 7
        assert await counter.inline(2) == 9
        with pytest.raises(pybeam.ActorStateAccessError):
            _ = counter.step


def test_constructor_defaults__are_not_materialized_outside_native_factory() -> None:
    constructions: list[int] = []

    def values() -> list[int]:
        constructions.append(1)
        return [10]

    @pybeam.actor
    class Defaults:
        items: list[int] = field(default_factory=values)

    spec = definition(Defaults)
    encoded = spec.constructor.encode((), {})
    assert encoded == b"{}"
    assert not constructions
    factory = spec.factory(encoded)
    assert not constructions
    factory()
    assert constructions == [1]


async def test_wrong_return__is_rejected_at_boundary(backend: ScriptedGroup) -> None:
    # Deliberately bypass static checking through the adapter boundary.
    async def wrong(state: object) -> object:
        return "wrong"

    from pybeam.actors.codec import Method

    method = definition(Counter).methods[2]
    invalid = Method(method.name, wrong, method.arguments, method.result)
    with pytest.raises(ActorPayloadError):
        await invalid.dispatch(object(), b"{}")


async def test_default_backend__fails_closed_without_bounded_native_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "erlang.actors", ModuleType("erlang.actors"))
    with pytest.raises(RuntimeError, match="no raw-object fallback"):
        async with pybeam.ActorGroup():
            pytest.fail("Unavailable bounded native groups must not be emulated")


def test_actor__rejects_properties_and_custom_metaclasses() -> None:
    class Properties:
        @property
        def value(self) -> int:
            return 1

    with pytest.raises(TypeError, match="descriptors"):
        pybeam.actor(Properties)

    class Reserved:
        async def _actor_route(self) -> int:
            return 1

    with pytest.raises(TypeError, match="Reserved"):
        pybeam.actor(Reserved)

    class Meta(type):
        pass

    class Custom(metaclass=Meta):
        pass

    with pytest.raises(TypeError, match="metaclasses"):
        pybeam.actor(Custom)
