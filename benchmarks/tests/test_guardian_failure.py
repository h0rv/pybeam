import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO, Literal

import psutil
import pytest
from pybeam.codec import Json

from benchmarks import runner
from benchmarks.contract import Budget, Config, Identity
from benchmarks.ownership import Lease, Registry, cleanup, identity, record, resolve, write_lease
from benchmarks.runner import ContaminatedLoad, measurement_lock, trial
from benchmarks.tests.test_runner import wait_for

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mode", ["known", "adapter_start", "broker_start"])
def test_guardian_death__cleans_registered_ids_under_lock_and_blocks_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    lock = tmp_path / "load.lock"
    monkeypatch.setattr(runner, "LOAD_LOCK", lock)
    original_spawn = subprocess.Popen

    def launch(
        argv: list[str],
        *,
        start_new_session: bool,
        pass_fds: tuple[int, ...],
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> subprocess.Popen[bytes]:
        if mode != "known":
            argv = [*argv[:2], "benchmarks.tests.guardian_fault", mode, *argv[3:]]
        return original_spawn(
            argv,
            start_new_session=start_new_session,
            pass_fds=pass_fds,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    entered = threading.Event()
    release = threading.Event()
    original_cleanup = runner.cleanup

    def paused_cleanup(owned: set[Identity]) -> tuple[Identity, ...]:
        entered.set()
        if not release.wait(timeout=10):
            raise TimeoutError("Test did not release fallback cleanup")
        return original_cleanup(owned)

    monkeypatch.setattr(runner, "cleanup", paused_cleanup)
    directory = tmp_path / "trial"
    config = Config(
        python_source=str(ROOT),
        native_source=str(ROOT),
        repeats=1,
        budget=Budget(wall_seconds=10.0),
        adapter_command=(sys.executable, "-m", "benchmarks.tests.probes", "child_wait"),
    )
    captured: set[Identity] = set()
    try:
        with measurement_lock() as descriptor, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(trial, config, directory, descriptor)
            try:
                if mode == "known":
                    wait_for(directory / "test-child.json")
                    registry = Json(Registry).decode((directory / "ownership.json").read_text())
                    assert len(registry.owned) >= 2
                    adapter = next(
                        value
                        for value in registry.owned
                        if "benchmarks.tests.probes" in resolve_command(value)
                    )
                    process = resolve(adapter)
                    assert process is not None
                    guardian = identity(process.parent)
                else:
                    wait_for(directory / "startup-owned.json")
                    registry = Json(Registry).decode((directory / "startup-owned.json").read_text())
                    assert registry.phase == "spawning"
                    guardian = Json(Identity).decode(
                        (directory / "startup-guardian.json").read_text()
                    )
                captured.update(registry.owned)
                target = resolve(guardian)
                assert target is not None
                os.kill(target.pid, signal.SIGKILL)
                assert entered.wait(timeout=10)
                with pytest.raises(RuntimeError, match="shared load lock"), measurement_lock():
                    pass
            finally:
                release.set()
            outcome = future.result(timeout=10)
            assert outcome.status == "failed"
            assert not outcome.cleanup_survivors
            assert all(resolve(value) is None for value in captured)
            assert outcome.cleanup_state == ("confirmed" if mode == "known" else "uncertain")
            with pytest.raises(RuntimeError, match="shared load lock"), measurement_lock():
                pass
        if mode == "known":
            with measurement_lock():
                pass
            assert lock.read_bytes() == b""
        else:
            for _ in range(2):
                with (
                    pytest.raises(ContaminatedLoad, match="Measurement contaminated"),
                    measurement_lock(),
                ):
                    pass
            assert lock.read_bytes()
    finally:
        release.set()
        cleanup(captured)


def resolve_command(value: Identity) -> list[str]:
    process = resolve(value)
    return [] if process is None else psutil.Process(process.pid).cmdline()


@pytest.mark.parametrize("phase", ["tracking", "spawning", "uncertain", "confirmed"])
def test_lease__never_releases_load_with_active_or_uncertain_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: Literal["tracking", "spawning", "uncertain", "confirmed"],
) -> None:
    monkeypatch.setattr(runner, "LOAD_LOCK", tmp_path / "load.lock")
    record(tmp_path, {identity(os.getpid())}, phase)
    with measurement_lock() as descriptor:
        write_lease(descriptor, Lease(trial_directory=str(tmp_path)))
    with pytest.raises(ContaminatedLoad), measurement_lock():
        pass


def test_lease__unreadable_registry_is_not_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "LOAD_LOCK", tmp_path / "load.lock")
    with measurement_lock() as descriptor:
        write_lease(descriptor, Lease(trial_directory=str(tmp_path)))
    (tmp_path / "ownership.json").write_text("partial")
    with pytest.raises(ContaminatedLoad), measurement_lock():
        pass
