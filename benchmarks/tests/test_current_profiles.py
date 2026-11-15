import hashlib
from pathlib import Path

import pytest

from benchmarks.contract import AdapterResult
from benchmarks.profiles import NAMES, ROOT, BenchmarkName, configuration, unavailable
from benchmarks.pybeam_reference_build import BASELINE, BuildManifest, verify
from benchmarks.pybeam_reference_worker import WorkerEvidence
from benchmarks.runner import measurement_lock, trial


@pytest.mark.parametrize("name", NAMES)
def test_profiles__fixed_portable_commands_and_scalar_caps(name: BenchmarkName) -> None:
    config = configuration(name)
    assert config.adapter_command[0] == str(ROOT / "benchmarks/.venv/bin/python")
    assert config.python_source == str(ROOT)
    assert config.workload.entities == 2
    assert config.workload.events == 100
    assert config.workload.warmup_events == 17
    assert config.workload.max_inflight == 4
    assert config.workload.queue_bound_scope == "client_admission_only"
    assert config.budget.wall_seconds == 15.0
    assert config.host_control == "uncontrolled_smoke"
    if name != "pybeam":
        assert config.native_source is None


def test_profiles__missing_native_assets_are_not_a_pass(tmp_path: Path) -> None:
    assert unavailable("pybeam", None) is not None
    assert unavailable("pybeam", tmp_path) is not None


def test_current_runtime__loads_real_c_and_releases_all_actor_contexts(tmp_path: Path) -> None:
    native = ROOT.parent / "native-benchmark-current"
    build = native / "_build/pybeam-reference-build.json"
    if not build.is_file():
        pytest.skip("Requires the explicitly built private current native profile")
    manifest = BuildManifest.model_validate_json(build.read_text())
    verify(manifest, native)
    assert manifest.native.sha == BASELINE
    config = configuration("pybeam", native)
    directory = tmp_path / "current-native-c"
    with measurement_lock() as descriptor:
        outcome = trial(config, directory, descriptor)
    assert outcome.status == "passed", outcome
    assert outcome.cleanup_state == "confirmed"
    assert not outcome.cleanup_survivors
    assert outcome.forced_cleanup_count == 0
    result = AdapterResult.model_validate_json((directory / "adapter.json").read_text())
    assert result.runtime.native_source_sha == BASELINE
    assert result.runtime.mode.startswith("raw-unbounded-current-runtime/")
    assert result.runtime.interpreters == 1
    assert result.runtime.gil_enabled is True
    assert result.runtime.schedulers == 1
    assert result.terminal_counts == (50, 50)
    evidence = WorkerEvidence.model_validate_json((directory / "worker-evidence.json").read_text())
    before, active, stopped = evidence.nif_contexts
    assert active.live == before.live + 2
    assert stopped.live == before.live
    assert stopped.destroyed == before.destroyed + 2
    assert evidence.stopped.contexts == evidence.baseline.contexts
    assert not evidence.stopped.actors
    assert evidence.actor_state_collected
    for item in result.runtime.loaded_assets:
        assert Path(item.path).is_relative_to(native)
        assert hashlib.sha256(Path(item.path).read_bytes()).hexdigest() == item.sha256
