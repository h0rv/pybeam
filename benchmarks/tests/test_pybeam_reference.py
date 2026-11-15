import asyncio
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Concatenate

import pytest

from benchmarks.contract import Config, Source, Unsupported, Workload, messages
from benchmarks.pybeam_reference import run, unsupported
from benchmarks.pybeam_reference_build import BASELINE, BuildManifest, asset, build, verify
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
        actors = [
            LocalActorDouble(calls, room=Room(entity=0)),
            LocalActorDouble(calls, room=Room(entity=1)),
        ]
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
        actors = [LocalActorDouble(calls, room=Room(count=9, entity=0))]
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


@pytest.mark.parametrize("budget", [None, "", "1", "2", "10", "invalid"])
def test_private_build__does_not_change_existing_project_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str | None
) -> None:
    if budget is None:
        monkeypatch.delenv("CMAKE_BUILD_PARALLEL_LEVEL", raising=False)
    else:
        monkeypatch.setenv("CMAKE_BUILD_PARALLEL_LEVEL", budget)
    project = tmp_path / "project"
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    checkout = project / "_checkouts/erlang_python"
    checkout.parent.mkdir(parents=True)
    checkout.symlink_to(canonical, target_is_directory=True)
    monkeypatch.setattr(
        "benchmarks.pybeam_reference_build.__file__",
        str(project / "benchmarks/pybeam_reference_build.py"),
    )
    native = tmp_path / "private"
    for name in (
        "src/erlang_python.app.src",
        "priv/py_nif.so",
        "_build/cmake/CMakeCache.txt",
    ):
        path = native / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private build fixture")
    python = tmp_path / "python"
    python.touch()
    python.with_name("python3.14-config").touch()
    expected = Source(
        path=str(native),
        sha=BASELINE,
        dirty=False,
        diff_sha256="0" * 64,
        working_tree_sha256="0" * 64,
    )

    def source(_path: Path) -> Source:
        return expected

    commands: list[tuple[str, ...]] = []

    def command(
        argv: tuple[str, ...], *, env: dict[str, str], check: bool, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append(argv)
        assert env["PYTHON_CONFIG"] == str(python.with_name("python3.14-config"))
        assert env["CMAKE_BUILD_PARALLEL_LEVEL"] == (budget or "2")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("benchmarks.pybeam_reference_build.source", source)
    monkeypatch.setattr("benchmarks.pybeam_reference_build.subprocess.run", command)
    if budget not in {None, "", "1", "2"}:
        with pytest.raises(ValueError, match="must be 1 or 2"):
            build(native, python)
        assert not commands
        assert checkout.resolve() == canonical
        return
    manifest = BuildManifest.model_validate_json(build(native, python).read_text())
    assert manifest.native == expected
    assert manifest.commands == tuple(commands)
    assert commands[1][-2:] == ("--parallel", budget or "2")
    assert checkout.resolve() == canonical
    assert Path(manifest.runtime_directory).is_relative_to(native)


@pytest.mark.parametrize(
    "escape",
    [
        "canonical",
        "priv",
        "priv/py_nif.so",
        "_build",
        "c_src",
        "_build/default/lib/erlang_python/ebin/erlang_python.app",
        "_build/default/lib/erlang_python/ebin/py_launch.beam",
    ],
)
def test_private_build__rejects_canonical_or_escaping_outputs_before_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, escape: str
) -> None:
    native, canonical = tmp_path / "private", tmp_path / "canonical"
    native.mkdir()
    canonical.mkdir()
    monkeypatch.setattr("benchmarks.pybeam_reference_build.CANONICAL_NATIVE", canonical)
    if escape == "canonical":
        native = canonical
    else:
        target = native / escape
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(canonical, target_is_directory=True)

    def forbidden_source(_path: Path) -> Source:
        pytest.fail("Unsafe roots must be rejected before source inspection or compilation")

    monkeypatch.setattr("benchmarks.pybeam_reference_build.source", forbidden_source)
    with pytest.raises(ValueError, match=r"canonical|escapes"):
        build(native, tmp_path / "unused-python")
