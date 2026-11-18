import asyncio
import sys
from types import ModuleType
from typing import Literal

import pytest

from pybeam.codec import Json
from pybeam.tasks import ExecutionMode, TaskCrashed, TaskFunction, TaskLimits, TaskSupervisor
from tests.test_tasks import byte_entry


class NativeTaskCrashed(RuntimeError):
    def __init__(self, reason: object) -> None:
        self.reason = reason
        super().__init__(reason)


class NativeUncertainInflight(RuntimeError):
    pass


class TasksModule(ModuleType):
    TaskCrashed = NativeTaskCrashed
    UncertainInflight = NativeUncertainInflight

    def __init__(self) -> None:
        super().__init__("erlang.tasks")
        self.calls: list[tuple[object, bytes, str]] = []
        self.failure: BaseException | None = None
        self.termination_blocked = False

    async def thread_snapshot(self, group: object, pid: object) -> tuple[int, int, bytes]:
        return 1234, 125_000_000, b"bounded stack\xff"

    async def terminate(self, group: object, pid: object) -> None:
        if self.termination_blocked:
            await asyncio.Event().wait()
        self.failure = NativeTaskCrashed(("signal", 9))

    async def open_group(self, count: int, max_bytes: int) -> tuple[object, object]:
        return object(), object()

    async def start(
        self, group: object, gate: object, factory: object, payload: bytes, *, mode: str = "worker"
    ) -> object:
        self.calls.append((factory, payload, mode))
        return object()

    async def outcome(
        self, group: object, pid: object
    ) -> tuple[Literal[True], bytes] | tuple[Literal[False], BaseException]:
        if self.failure is not None:
            return False, self.failure
        return True, self.calls[-1][1]

    async def shutdown(self, group: object, milliseconds: int) -> None:
        pass

    async def dispose(self, group: object) -> None:
        pass


class ActorsModule(ModuleType):
    ActorOverloaded = RuntimeError


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> TasksModule:
    facade = TasksModule()
    monkeypatch.setitem(sys.modules, "erlang", ModuleType("erlang"))
    monkeypatch.setitem(sys.modules, "erlang.tasks", facade)
    monkeypatch.setitem(sys.modules, "erlang.actors", ActorsModule("erlang.actors"))
    return facade


@pytest.mark.parametrize("execution", [ExecutionMode.OWNGIL, ExecutionMode.ISOLATED])
async def test_tasks__dispatch_named_mode_with_caller_codecs(
    native: TasksModule, execution: Literal[ExecutionMode.OWNGIL, ExecutionMode.ISOLATED]
) -> None:
    function = TaskFunction(byte_entry, Json(bytes), Json(bytes), execution=execution)
    async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=100, shutdown_ms=1000)) as sup:
        ref = await sup.start(function, b"")
        assert await ref == b""
        assert native.calls == [(byte_entry, Json(bytes).encode(b""), execution.value)]


async def test_tasks__expose_and_cache_crash_exception(native: TasksModule) -> None:
    native.failure = NativeTaskCrashed(("signal", 6))
    function = TaskFunction(byte_entry, Json(bytes), Json(bytes), execution=ExecutionMode.ISOLATED)
    async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=100, shutdown_ms=1000)) as sup:
        ref = await sup.start(function, b"payload")
        error = await ref.exception()
        assert isinstance(error, TaskCrashed)
        assert error.reason == ("signal", 6)
        with pytest.raises(TaskCrashed) as raised:
            await ref.result()
        assert raised.value is error


async def test_tasks__preserve_other_backend_errors(native: TasksModule) -> None:
    native.failure = ValueError("remote error")
    function = TaskFunction(byte_entry, Json(bytes), Json(bytes), execution=ExecutionMode.ISOLATED)
    async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=100, shutdown_ms=1000)) as sup:
        ref = await sup.start(function, b"payload")
        assert await ref.exception() is native.failure


@pytest.mark.parametrize("execution", [ExecutionMode.WORKER, ExecutionMode.OWNGIL])
async def test_tasks__reject_inspection_and_termination_without_isolation(
    native: TasksModule, execution: ExecutionMode
) -> None:
    function = TaskFunction(byte_entry, Json(bytes), Json(bytes), execution=execution)
    async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=100, shutdown_ms=1000)) as sup:
        ref = await sup.start(function, b"payload")
        with pytest.raises(ValueError, match="isolated execution"):
            await ref.inspect()
        with pytest.raises(ValueError, match="isolated execution"):
            await ref.terminate()
        assert await ref == b"payload"


async def test_tasks__decode_bounded_snapshot_and_preserve_terminated_outcome(
    native: TasksModule,
) -> None:
    function = TaskFunction(byte_entry, Json(bytes), Json(bytes), execution=ExecutionMode.ISOLATED)
    async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=100, shutdown_ms=1000)) as sup:
        ref = await sup.start(function, b"payload")
        snapshot = await ref.inspect()
        assert snapshot.os_pid == 1234
        assert snapshot.cpu_seconds == 0.125
        assert snapshot.stacks == "bounded stack\ufffd"
        await ref.terminate()
        error = await ref.exception()
        assert isinstance(error, TaskCrashed)
        with pytest.raises(TaskCrashed) as raised:
            await ref.result()
        assert raised.value is error


async def test_tasks__termination_deadline_is_uncertain_not_success(native: TasksModule) -> None:
    native.termination_blocked = True
    function = TaskFunction(byte_entry, Json(bytes), Json(bytes), execution=ExecutionMode.ISOLATED)
    async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=100, shutdown_ms=10)) as sup:
        ref = await sup.start(function, b"payload")
        with pytest.raises(NativeUncertainInflight, match="did not confirm child exit"):
            await ref.terminate()
        assert await ref == b"payload"
