import contextlib
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from pybeam.codec import Json

from benchmarks.contract import Budget, Config, Identity, Outcome, ResourceSample
from benchmarks.ownership import resolve
from benchmarks.runner import measurement_lock, run, summarize, trial


@pytest.fixture
def config() -> Config:
    root = Path(__file__).resolve().parents[2]
    return Config(
        python_source=str(root),
        native_source=str(root.parent / "erlang-python"),
        repeats=1,
    )


def probe(config: Config, mode: str, budget: Budget) -> Config:
    return config.model_copy(
        update={
            "adapter_command": (sys.executable, "-m", "benchmarks.tests.probes", mode),
            "budget": budget,
        }
    )


def test_runner__writes_raw_evidence_and_repeats(config: Config, tmp_path: Path) -> None:
    result = run(config.model_copy(update={"repeats": 2}), tmp_path / "result")
    assert len(result.trials) == 2
    assert all(t.outcome.status == "passed" for t in result.trials)
    assert result.throughput_repeat_statistics is not None
    assert result.throughput_repeat_statistics.count == 2
    assert (tmp_path / "result/provenance.json").exists()
    samples = (tmp_path / "result/trial-0/resources.jsonl").read_text().splitlines()
    assert all(ResourceSample.model_validate_json(line) for line in samples)


def test_lock__serializes_workstreams() -> None:
    with (
        measurement_lock(),
        pytest.raises(RuntimeError, match="shared load lock"),
        measurement_lock(),
    ):
        pass


@pytest.mark.parametrize(
    ("mode", "budget", "reason"),
    [
        ("hang", Budget(wall_seconds=0.3), "Wall-time"),
        ("memory", Budget(tree_rss_bytes=40_000_000), "RSS"),
        ("child_wait", Budget(processes=1), "process count"),
        ("threads", Budget(threads=1), "thread count"),
        ("artifact", Budget(artifact_bytes=100_000), "Artifact"),
    ],
)
def test_budget__stops_owned_work(
    config: Config,
    tmp_path: Path,
    mode: str,
    budget: Budget,
    reason: str,
) -> None:
    selected = probe(config, mode, budget)
    with measurement_lock() as descriptor:
        outcome = trial(selected, tmp_path / "trial", descriptor)
    assert outcome.status == "failed"
    assert outcome.reason is not None and reason in outcome.reason
    assert not outcome.cleanup_survivors
    summary = summarize(0, tmp_path / "trial", selected, outcome)
    assert summary.completed is None and summary.throughput_per_second is None


def test_adapter_death__reaps_broker_owned_detached_child(config: Config, tmp_path: Path) -> None:
    selected = probe(config, "child_exit", Budget())
    with measurement_lock() as descriptor:
        outcome = trial(selected, tmp_path / "trial", descriptor)
    child = Json(Identity).decode((tmp_path / "trial/test-child.json").read_text())
    assert outcome.status == "failed"
    assert not outcome.cleanup_survivors
    assert resolve(child) is None


@pytest.mark.parametrize("mode", ["bad_evidence", "false_cleanup"])
def test_runner__rejects_false_success(config: Config, tmp_path: Path, mode: str) -> None:
    selected = probe(config, mode, Budget())
    with measurement_lock() as descriptor:
        outcome = trial(selected, tmp_path / "trial", descriptor)
    assert outcome.status == "failed"
    assert not outcome.cleanup_survivors
    if mode == "false_cleanup":
        assert outcome.forced_cleanup_count > 0
        child = Json(Identity).decode((tmp_path / "trial/test-child.json").read_text())
        assert resolve(child) is None


def wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(str(path))
        time.sleep(0.02)


def test_harness_death__guardian_keeps_lock_and_reaps_vm(config: Config, tmp_path: Path) -> None:
    selected = probe(config, "child_wait", Budget(wall_seconds=5.0))
    config_path = tmp_path / "config.json"
    config_path.write_text(selected.model_dump_json())
    # The CLI permits /tmp; pytest's private temporary directory is platform dependent.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="pybeam-orphan-test-", dir="/tmp") as temporary:
        output = Path(temporary) / "result"
        with (tmp_path / "host.log").open("wb") as log:
            host = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "benchmarks",
                    "run",
                    "--config",
                    str(config_path),
                    "--output",
                    str(output),
                ],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                child_path = output / "trial-0/test-child.json"
                wait_for(child_path)
                child = Json(Identity).decode(child_path.read_text())
                with pytest.raises(RuntimeError, match="shared load lock"), measurement_lock():
                    pass
                host.kill()
                host.wait(timeout=3)
                wait_for(output / "trial-0/outcome.json")
                outcome = Outcome.model_validate_json((output / "trial-0/outcome.json").read_text())
                assert outcome.reason == "Harness owner exited"
                assert not outcome.cleanup_survivors
                assert resolve(child) is None
            finally:
                with contextlib.suppress(ProcessLookupError):
                    host.kill()
                host.wait(timeout=3)


def test_identity__does_not_resolve_reused_pid() -> None:

    from benchmarks.ownership import identity

    actual = identity(os.getpid())
    assert resolve(replace(actual, created=actual.created - 1)) is None
