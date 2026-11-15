import hashlib
import sys
from collections import Counter
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from benchmarks.contract import (
    AdapterOutput,
    AdapterResult,
    Config,
    Outcome,
    Receipt,
    StrictModel,
    Unsupported,
    Workload,
    check_correctness,
    messages,
)
from benchmarks.native_reference.__main__ import BUILD, BuildManifest, unsupported
from benchmarks.runner import measurement_lock, trial

ROOT = Path(__file__).resolve().parents[2]


class ResetProof(StrictModel):
    warmup_counts: tuple[int, ...]
    reset_counts: tuple[int, ...]
    same_workers: bool
    worker_pids: tuple[str, ...]


class WorkerDown(StrictModel):
    pid: str
    reason: str


class DeletionProof(StrictModel):
    downs: tuple[WorkerDown, ...]
    deleted: int
    all_dead: bool


def config(*, failure: bool = False, warmup: int = 17) -> Config:
    command = (sys.executable, "-m", "benchmarks.native_reference")
    if failure:
        command += ("--probe-failure",)
    return Config(
        backend="erlang",
        adapter_command=command,
        python_source=str(ROOT),
        native_source=str(ROOT.parent / "native-benchmarks"),
        workload=Workload(
            entities=2,
            events=100,
            warmup_events=warmup,
            max_inflight=4,
            queue_capacity=4,
            queue_bound_scope="client_admission_only",
        ),
        repeats=1,
    )


@pytest.fixture(scope="module")
def native_trials(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    if not (BUILD / "manifest.json").exists():
        pytest.skip("Build private OTP assets with python -m benchmarks.native_reference --build")
    root = tmp_path_factory.mktemp("native-reference")
    paths = (root / "success", root / "zero-warmup", root / "failure")
    # Unit correctness only. Fail immediately if another measured workload owns the lock.
    with measurement_lock() as descriptor:
        for directory, request in zip(
            paths, (config(), config(warmup=0), config(failure=True)), strict=True
        ):
            outcome = trial(request, directory, descriptor)
            (directory / "test-outcome.json").write_text(outcome.model_dump_json())
    return paths


def test_native_completion__strict_receipts_reset_snapshot_deletion(
    native_trials: tuple[Path, Path, Path],
) -> None:
    for directory in native_trials[:2]:
        request = Config.model_validate_json((directory / "config.json").read_text())
        result = AdapterResult.model_validate_json((directory / "native-result.json").read_text())
        receipts = tuple(
            Receipt.model_validate_json(line)
            for line in (directory / "native-receipts.jsonl").read_text().splitlines()
        )
        assert receipts == result.receipts
        expected = messages(request.workload, request.workload.events)
        assert Counter((r.id, r.entity) for r in receipts) == Counter(
            (m.id, m.entity) for m in expected
        )
        assert result.terminal_counts == (50, 50)
        assert result.entities_created == result.entities_deleted == 2
        assert result.cleanup_acknowledged
        assert result.inflight_peak == 4
        assert result.queue_peak is None
        assert result.transport_payload_bytes is None
        assert not result.failures and not result.lost
        assert result.phases.measurement_ns >= max(r.completed_ns for r in receipts) - min(
            r.sent_ns for r in receipts
        )
        reset = ResetProof.model_validate_json((directory / "reset-proof.json").read_text())
        counts = Counter(
            m.entity for m in messages(request.workload, request.workload.warmup_events)
        )
        assert reset.warmup_counts == (counts[0], counts[1])
        assert reset.reset_counts == (0, 0) and reset.same_workers
        deletion = DeletionProof.model_validate_json(
            (directory / "deletion-proof.json").read_text()
        )
        assert deletion.all_dead is True
        assert deletion.deleted == len(deletion.downs) == 2
        assert reset.worker_pids == tuple(item.pid for item in deletion.downs)
        assert len(set(reset.worker_pids)) == 2
        assert all(item.reason == "shutdown" for item in deletion.downs)
        for receipt in receipts:
            assert receipt.completed_ns >= receipt.sent_ns >= 0
        with pytest.raises(ValidationError):
            Receipt.model_validate_json('{"id":true,"entity":0,"sent_ns":0,"completed_ns":1}')
        with pytest.raises(ValidationError):
            Receipt.model_validate_json('{"id":0,"entity":0,"sent_ns":2,"completed_ns":1}')


def test_native_runtime__honest_provenance_and_schema_blocker(
    native_trials: tuple[Path, Path, Path],
) -> None:
    directory = native_trials[0]
    result = AdapterResult.model_validate_json((directory / "native-result.json").read_text())
    runtime = result.runtime
    assert runtime.otp == "29.1.2"
    assert runtime.erts is not None and runtime.schedulers == 1
    assert runtime.python is runtime.elixir is runtime.gil_enabled is runtime.interpreters is None
    assert runtime.native_source_sha is None
    assert runtime.mode == "native_otp_gen_server_local_single_vm"
    assert (
        hashlib.sha256(Path(runtime.executable).read_bytes()).hexdigest()
        == runtime.executable_sha256
    )
    manifest = BuildManifest.model_validate_json((directory / "build-manifest.json").read_text())
    assert manifest.assets == runtime.loaded_assets
    assert not manifest.pybeam_nif_loaded and not manifest.paired_native_checkout_used
    for item in manifest.assets + manifest.sources:
        assert hashlib.sha256(Path(item.path).read_bytes()).hexdigest() == item.sha256
    with pytest.raises(ValueError, match="Native runtime metadata is incomplete"):
        check_correctness(config(), result)
    output = TypeAdapter(AdapterOutput).validate_json((directory / "adapter.json").read_text())
    assert isinstance(output, Unsupported)
    outcome = Outcome.model_validate_json((directory / "test-outcome.json").read_text())
    assert outcome.status == "unsupported"
    assert outcome.forced_cleanup_count == 0 and not outcome.cleanup_survivors


def test_native_failure__owned_vm_exits_and_workers_are_down(
    native_trials: tuple[Path, Path, Path],
) -> None:
    directory = native_trials[2]
    outcome = Outcome.model_validate_json((directory / "test-outcome.json").read_text())
    assert outcome.status == "failed" and outcome.returncode != 0
    assert not outcome.cleanup_survivors and outcome.forced_cleanup_count == 0
    assert (directory / "failure.json").exists()
    assert not (directory / "native-result.json").exists()
    deletion = DeletionProof.model_validate_json((directory / "deletion-proof.json").read_text())
    assert deletion.deleted == 2 and deletion.all_dead
    assert any("injected_handler_failure" in item.reason for item in deletion.downs)


def test_native_capabilities__reject_unimplemented_semantics() -> None:
    base = config()
    for workload in (
        Workload(),
        Workload(id="rooms.churn", queue_bound_scope="client_admission_only"),
        Workload(activity="idle", queue_bound_scope="client_admission_only"),
    ):
        assert unsupported(base.model_copy(update={"workload": workload})) is not None
