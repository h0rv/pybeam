import sys
from pathlib import Path

import pytest

from benchmarks.contract import AdapterResult, Config, Outcome
from benchmarks.pybeam_reference_build import BuildManifest
from benchmarks.runner import measurement_lock, summarize, trial
from benchmarks.tests.identity_fault import Fault, RecordedReplies

ROOT = Path(__file__).resolve().parents[2]
FAULTS = (
    *(
        Fault(backend=backend, mode=mode)
        for backend in ("python", "pybeam", "smoke")
        for mode in ("route", "duplicate_id", "substitute_id")
    ),
    Fault(backend="pybeam", mode="count"),
)


def request(fault: Fault, native_source: Path) -> Config:
    backend = "python" if fault.backend == "smoke" else fault.backend
    config = Config.model_validate_json(
        (ROOT / "benchmarks/examples" / ("serial_" + backend + ".json")).read_text()
    )
    return config.model_copy(
        update={
            "repeats": 1,
            "python_source": str(ROOT),
            "native_source": str(native_source) if backend == "pybeam" else None,
            "adapter_command": (
                sys.executable,
                "-m",
                "benchmarks.tests.identity_fault",
                fault.model_dump_json(),
            ),
        }
    )


def verify_failure(directory: Path, fault: Fault, config: Config, outcome: Outcome) -> None:
    assert outcome.status == "failed"
    assert outcome.cleanup_state == "confirmed"
    assert not outcome.cleanup_survivors and outcome.forced_cleanup_count == 0
    summary = summarize(0, directory, config, outcome)
    assert summary.completed is summary.throughput_per_second is summary.latency_ns is None
    if fault.backend == "smoke" or fault.full_delivery:
        result = AdapterResult.model_validate_json((directory / "adapter.json").read_text())
        assert result.terminal_counts == (50, 50)
        assert len(result.receipts) == 100
        if fault.mode == "route":
            assert all(r.entity != (r.id + 1) % 2 for r in result.receipts)
        elif fault.mode == "duplicate_id":
            assert all(r.id == 0 for r in result.receipts)
        else:
            assert all(r.id >= 100 for r in result.receipts)
    else:
        evidence = RecordedReplies.model_validate_json(
            (directory / "received-replies.json").read_text()
        )
        assert evidence.fault == fault and evidence.replies
        assert all(reply.count > 0 for reply in evidence.replies)
        if fault.mode == "route":
            assert any(reply.entity != (reply.id + 1) % 2 for reply in evidence.replies)
        elif fault.mode == "duplicate_id":
            assert all(reply.id == 0 for reply in evidence.replies)
        elif fault.mode == "substitute_id":
            assert all(reply.id >= 100 for reply in evidence.replies)
        logs = "\n".join(path.read_text() for path in directory.glob("*stderr.log"))
        assert (
            "Invalid applied acknowledgment"
            if fault.mode == "count"
            else "wrong message or executing entity identity"
        ) in logs


FULL_FAULTS = tuple(
    Fault(backend=backend, mode=mode, full_delivery=True)
    for backend in ("python", "pybeam")
    for mode in ("route", "duplicate_id", "substitute_id")
)


@pytest.mark.parametrize(
    "fault",
    (*FAULTS, *FULL_FAULTS),
    ids=lambda fault: fault.backend + "-" + fault.mode + ("-full" if fault.full_delivery else ""),
)
def test_actual_delivery__rejects_identity_corruption_under_guardian(
    tmp_path: Path,
    fault: Fault,
    native_source: Path,
) -> None:
    if fault.backend == "pybeam":
        manifest = native_source / "_build/pybeam-reference-build.json"
        if not manifest.exists():
            pytest.skip("Private current native build is required")
        BuildManifest.model_validate_json(manifest.read_text())
    config = request(fault, native_source)
    directory = tmp_path / (fault.backend + "-" + fault.mode)
    with measurement_lock() as descriptor:
        outcome = trial(config, directory, descriptor)
    verify_failure(directory, fault, config, outcome)
