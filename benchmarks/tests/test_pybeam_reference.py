import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Concatenate

import pytest

from benchmarks.contract import Config, Source, Unsupported, Workload, messages
from benchmarks.pybeam_reference import run, unsupported
from benchmarks.pybeam_reference_build import BASELINE, BuildManifest, asset, verify
from benchmarks.pybeam_reference_worker import Room, produce


@dataclass(slots=True)
class Calls:
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    active: int = 0
    peak: int = 0


@dataclass(slots=True)
class LocalActorDouble:
    calls: Calls
    fail: bool = False
    room: Room = field(default_factory=Room)

    async def call[**P, R](
        self,
        method: Callable[Concatenate[Room, P], Awaitable[R]],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        self.calls.active += 1
        self.calls.peak = max(self.calls.peak, self.calls.active)
        if self.calls.active == 2:
            self.calls.ready.set()
        try:
            await self.calls.gate.wait()
            if self.fail:
                raise ValueError("unit test handler failure")
            return await method(self.room, *args, **kwargs)
        finally:
            self.calls.active -= 1


def test_produce__bounds_tasks_and_includes_admission_wait_in_latency() -> None:
    async def check() -> None:
        calls = Calls()
        actors = [LocalActorDouble(calls), LocalActorDouble(calls)]
        workload = Workload(entities=2, events=30, seed=5)
        async with asyncio.timeout(2), asyncio.TaskGroup() as group:
            task = group.create_task(produce(actors, messages(workload, 30), 2))
            await calls.ready.wait()
            await asyncio.sleep(0)
            assert calls.active == 2
            released_ns = time.monotonic_ns()
            calls.gate.set()
        receipts, peak = task.result()
        assert peak == calls.peak == 2
        assert calls.active == 0
        assert sorted(r.id for r in receipts) == list(range(30))
        assert all(r.entity == (r.id + 5) % 2 for r in receipts)
        third = next(r for r in receipts if r.id == 2)
        assert third.sent_ns < released_ns <= third.completed_ns
        assert [actor.room.count for actor in actors] == [15, 15]
        for actor in actors:
            await actor.call(Room.reset)
        assert [actor.room.count for actor in actors] == [0, 0]

    asyncio.run(check())


def test_produce__failure_leaves_no_inflight_calls_or_producer_tasks() -> None:
    async def check() -> None:
        calls = Calls()
        calls.gate.set()
        actors = [LocalActorDouble(calls, fail=True)]
        with pytest.raises(ExceptionGroup):
            await produce(actors, messages(Workload(entities=1), 1000), 2)
        assert calls.active == 0
        current = asyncio.current_task()
        assert all(task is current for task in asyncio.all_tasks())

    asyncio.run(check())


def test_produce__rejects_bad_applied_acknowledgment() -> None:
    async def check() -> None:
        calls = Calls()
        calls.gate.set()
        actors = [LocalActorDouble(calls, room=Room(count=9))]
        with pytest.raises(ExceptionGroup, match="TaskGroup"):
            await produce(actors, messages(Workload(entities=1), 1), 1)
        assert calls.active == 0

    asyncio.run(check())


@pytest.mark.parametrize(
    "workload",
    [
        Workload(),
        Workload(id="rooms.churn", queue_bound_scope="client_admission_only"),
        Workload(activity="hot_10_percent", queue_bound_scope="client_admission_only"),
    ],
)
def test_adapter__unsupported_requests_never_launch_a_vm(
    tmp_path: Path,
    workload: Workload,
) -> None:
    config = Config(
        backend="pybeam",
        adapter_command=("unused",),
        workload=workload,
        python_source=str(tmp_path),
        native_source=str(tmp_path),
    )
    (tmp_path / "config.json").write_text(config.model_dump_json())
    assert isinstance(run(tmp_path), Unsupported)


def test_adapter__accepts_client_admission_without_claiming_a_mailbox_bound() -> None:
    config = Config(
        backend="pybeam",
        adapter_command=("unused",),
        workload=Workload(queue_bound_scope="client_admission_only"),
        python_source="unused",
        native_source="unused",
    )
    assert unsupported(config) is None


def test_build_manifest__rejects_changed_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fixture.asset"
    path.write_text("verified initial bytes")
    native = Source(
        path=str(tmp_path),
        sha=BASELINE,
        dirty=False,
        diff_sha256="0" * 64,
        working_tree_sha256="0" * 64,
    )
    manifest = BuildManifest(
        native=native,
        runtime_directory=str(tmp_path),
        python_executable=asset(path),
        cmake_cache=asset(path),
        assets=(asset(path),),
        commands=(),
    )

    def unchanged_source(_path: Path) -> Source:
        return native

    monkeypatch.setattr("benchmarks.pybeam_reference_build.source", unchanged_source)
    verify(manifest, tmp_path)
    path.write_text("changed bytes")
    with pytest.raises(ValueError, match="Build asset changed"):
        verify(manifest, tmp_path)
