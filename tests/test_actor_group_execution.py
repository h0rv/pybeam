import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Self, override

import pytest

from pybeam import ActorGroup, ExecutionLimits, actor
from pybeam.actors import groups
from pybeam.actors.classes import Container, DataActor
from tests.test_pythonic_actors import ScriptedActor


@dataclass(frozen=True)
class NativeLimits:
    workers: int
    capacity: int
    max_bytes: int


class NativeGroup:
    def __init__(self, *, max_children: int, execution: NativeLimits | None = None) -> None:
        self.max_children = max_children
        self.execution = execution
        self.entered = False
        self.closed = False
        self.admission: list[tuple[int, int]] = []

    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    async def spawn(
        self,
        child_id: str,
        factory: Callable[[], Container],
        *,
        capacity: int = 64,
        max_bytes: int = 1_048_576,
    ) -> DataActor:
        self.admission.append((capacity, max_bytes))
        return ScriptedActor(factory(), child_id)

    async def stop(self, child_id: str, /) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class DedicatedModule(ModuleType):
    DataActorGroup: type[NativeGroup]


class PooledModule(DedicatedModule):
    PoolLimits = NativeLimits


@dataclass
class NativeHarness:
    constructed: list[NativeGroup] = field(default_factory=list)
    enter_error: RuntimeError | None = None


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> NativeHarness:
    harness = NativeHarness()

    class RecordedGroup(NativeGroup):
        def __init__(self, *, max_children: int, execution: NativeLimits | None = None) -> None:
            super().__init__(max_children=max_children, execution=execution)
            harness.constructed.append(self)

        @override
        async def __aenter__(self) -> Self:
            if harness.enter_error is not None:
                # Native __aenter__ owns cleanup of resources it acquires before failing.
                await self.close()
                raise harness.enter_error
            return await super().__aenter__()

    facade = PooledModule("erlang.actors")
    facade.DataActorGroup = RecordedGroup
    monkeypatch.setitem(sys.modules, "erlang", ModuleType("erlang"))
    monkeypatch.setitem(sys.modules, "erlang.actors", facade)
    return harness


@actor
class Counter:
    value: int = 0

    async def add(self) -> int:
        self.value += 1
        return self.value


async def test_execution__passes_exact_native_limits_and_preserves_per_actor_limits(
    native: NativeHarness,
) -> None:
    execution = ExecutionLimits(workers=4, capacity=32, max_bytes=1_048_576)
    group = ActorGroup(max_children=800, max_pending=8, max_bytes=65_536, execution=execution)
    assert group._execution is execution
    async with group:
        counter = await group.spawn(Counter)
        assert await counter.add() == 1
        (backend,) = native.constructed
        assert backend.max_children == 800
        assert backend.execution == NativeLimits(workers=4, capacity=32, max_bytes=1_048_576)
        assert backend.entered
        assert backend.admission == [(8, 65_536)]
        assert not backend.closed
    assert backend.closed


async def test_dedicated__supports_old_facade_without_pool_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[NativeGroup] = []

    class DedicatedGroup(NativeGroup):
        def __init__(self, *, max_children: int) -> None:
            super().__init__(max_children=max_children)
            constructed.append(self)

    facade = DedicatedModule("erlang.actors")
    facade.DataActorGroup = DedicatedGroup
    monkeypatch.setitem(sys.modules, "erlang", ModuleType("erlang"))
    monkeypatch.setitem(sys.modules, "erlang.actors", facade)
    async with ActorGroup(max_children=7, execution=None):
        (backend,) = constructed
        assert backend.max_children == 7
        assert backend.entered
    assert backend.closed


async def test_execution__missing_pool_limits_fails_before_construction_without_fallback(
    native: NativeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ForbiddenGroup(NativeGroup):
        def __init__(self, *, max_children: int, execution: NativeLimits | None = None) -> None:
            pytest.fail("Missing PoolLimits must fail before any native construction")

    facade = DedicatedModule("erlang.actors")
    facade.DataActorGroup = ForbiddenGroup
    monkeypatch.setitem(sys.modules, "erlang.actors", facade)
    group = ActorGroup(execution=ExecutionLimits(workers=1, capacity=2, max_bytes=3))
    with pytest.raises(RuntimeError, match="Pooled native actors are required") as raised:
        await group.__aenter__()
    assert isinstance(raised.value.__cause__, ImportError)
    assert native.constructed == []
    assert group._native is None
    assert group._starts is None
    await group.__aexit__(None, None, None)


async def test_execution__native_enter_failure_keeps_existing_cleanup_ownership(
    native: NativeHarness,
) -> None:
    error = RuntimeError("native enter failed")
    native.enter_error = error
    group = ActorGroup(execution=ExecutionLimits(workers=1, capacity=2, max_bytes=3))
    with pytest.raises(RuntimeError, match="native enter failed") as raised:
        await group.__aenter__()
    assert raised.value is error
    (backend,) = native.constructed
    assert backend.closed
    assert group._native is None
    assert group._starts is None
    await group.__aexit__(None, None, None)


async def test_dedicated__preserves_max_children_only_open_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = NativeGroup(max_children=9)

    async def open_group(*, max_children: int) -> groups.DataGroup:
        assert max_children == 9
        return backend

    monkeypatch.setattr(groups, "open_group", open_group)
    async with ActorGroup(max_children=9):
        pass
    assert backend.closed
