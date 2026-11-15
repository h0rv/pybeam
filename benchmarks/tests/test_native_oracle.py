"""Negative controls for runtime identity and completion evidence.

Property: relaxing the unused NIF requirement must not accept missing actual
runtime assets or invalid application evidence. The small identity case matrix
is enumerated directly; no performance or VM-mapping proof is implied.
"""

import asyncio
from pathlib import Path

import pytest

from benchmarks.contract import AdapterResult, Config, Runtime, check_correctness
from benchmarks.guardian import verify_runtime_assets
from benchmarks.pybeam_reference_build import asset
from benchmarks.python_smoke import run


@pytest.fixture
def evidence(tmp_path: Path) -> tuple[Config, AdapterResult]:
    config = Config(python_source=str(tmp_path), native_source=None)
    output = asyncio.run(run(config))
    assert isinstance(output, AdapterResult)
    executable = tmp_path / "beam.smp"
    executable.write_bytes(b"fixture ERTS, not an actual trial")
    beam = tmp_path / "room.beam"
    beam.write_bytes(b"fixture compiled adapter")
    source = tmp_path / "room.erl"
    source.write_bytes(b"fixture source")
    runtime = Runtime(
        python=None,
        otp="29.1.2",
        erts="17.1.1",
        elixir=None,
        mode="oracle-fixture",
        gil_enabled=None,
        interpreters=None,
        workers=16,
        schedulers=1,
        executable=str(executable),
        executable_sha256=asset(executable).sha256,
        native_source_sha=None,
        loaded_assets=(asset(beam), asset(source)),
    )
    request = config.model_copy(update={"backend": "erlang", "adapter_command": ("fixture",)})
    result = output.model_copy(update={"backend": "erlang", "runtime": runtime})
    return request, result


def test_pure_otp__accepts_actual_assets_without_paired_nif(
    evidence: tuple[Config, AdapterResult],
) -> None:
    config, result = evidence
    check_correctness(config, result)
    verify_runtime_assets(config, result.runtime)


@pytest.mark.parametrize(
    "damage",
    [
        "missing_beam",
        "missing_source",
        "missing_otp",
        "missing_erts",
        "missing_scheduler",
        "false_python",
        "false_nif_revision",
        "false_executable",
        "bad_ack",
        "missing_state",
        "missing_deletion",
        "missing_cleanup",
    ],
)
def test_pure_otp__rejects_incomplete_or_lying_evidence(
    evidence: tuple[Config, AdapterResult],
    damage: str,
) -> None:
    config, result = evidence
    runtime = result.runtime
    match damage:
        case "missing_beam":
            runtime = runtime.model_copy(update={"loaded_assets": runtime.loaded_assets[1:]})
        case "missing_source":
            runtime = runtime.model_copy(update={"loaded_assets": runtime.loaded_assets[:1]})
        case "missing_otp":
            runtime = runtime.model_copy(update={"otp": None})
        case "missing_erts":
            runtime = runtime.model_copy(update={"erts": None})
        case "missing_scheduler":
            runtime = runtime.model_copy(update={"schedulers": None})
        case "false_python":
            runtime = runtime.model_copy(update={"python": "3.14.10"})
        case "false_nif_revision":
            runtime = runtime.model_copy(update={"native_source_sha": "0" * 40})
        case "false_executable":
            runtime = runtime.model_copy(update={"executable": "/bin/python"})
        case "bad_ack":
            result = result.model_copy(update={"receipts": result.receipts[:-1]})
        case "missing_state":
            result = result.model_copy(update={"terminal_counts": ()})
        case "missing_deletion":
            result = result.model_copy(update={"entities_deleted": 0})
        case "missing_cleanup":
            result = result.model_copy(update={"cleanup_acknowledged": False})
    with pytest.raises(ValueError):
        check_correctness(config, result.model_copy(update={"runtime": runtime}))


@pytest.mark.parametrize("damage", ["missing_sha", "mismatched_sha", "missing_nif", "bad_nif_hash"])
def test_pybeam__still_requires_paired_revision_and_actual_nif_hash(
    evidence: tuple[Config, AdapterResult],
    tmp_path: Path,
    damage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, result = evidence
    nif = tmp_path / "py_nif.so"
    nif.write_bytes(b"fixture NIF bytes")
    config = config.model_copy(update={"backend": "pybeam", "native_source": str(tmp_path)})
    sha = "1" * 40

    def fixture_revision(_root: Path, *_args: str) -> bytes:
        return sha.encode()

    monkeypatch.setattr("benchmarks.guardian.git", fixture_revision)
    runtime = result.runtime.model_copy(
        update={
            "python": "3.14.10",
            "gil_enabled": True,
            "interpreters": 1,
            "native_source_sha": sha,
            "loaded_assets": (*result.runtime.loaded_assets, asset(nif)),
        }
    )
    match damage:
        case "missing_sha":
            runtime = runtime.model_copy(update={"native_source_sha": None})
        case "missing_nif":
            runtime = runtime.model_copy(update={"loaded_assets": result.runtime.loaded_assets})
        case "mismatched_sha":
            runtime = runtime.model_copy(update={"native_source_sha": "0" * 40})
        case "bad_nif_hash":
            runtime = runtime.model_copy(
                update={
                    "loaded_assets": (
                        asset(nif).model_copy(update={"sha256": "0" * 64}),
                        *result.runtime.loaded_assets,
                    )
                }
            )
    result = result.model_copy(
        update={
            "backend": "pybeam",
            "runtime": runtime,
            "workload": result.workload.model_copy(
                update={"queue_bound_scope": "client_admission_only"}
            ),
        }
    )
    config = config.model_copy(update={"workload": result.workload})
    with pytest.raises(ValueError):
        check_correctness(config, result)
        verify_runtime_assets(config, runtime)


@pytest.mark.parametrize("target", ["executable", "beam", "source"])
def test_asset_verification__rejects_changed_bytes(
    evidence: tuple[Config, AdapterResult],
    target: str,
) -> None:
    config, result = evidence
    runtime = result.runtime
    paths = {
        "executable": runtime.executable,
        "beam": runtime.loaded_assets[0].path,
        "source": runtime.loaded_assets[1].path,
    }
    Path(paths[target]).write_bytes(b"changed after attestation")
    with pytest.raises(ValueError, match="hash differs"):
        verify_runtime_assets(config, runtime)


def test_backend_label__cannot_hide_declared_native_runtime(
    evidence: tuple[Config, AdapterResult],
) -> None:
    config, result = evidence
    config = config.model_copy(update={"backend": "python"})
    runtime = result.runtime.model_copy(
        update={"python": "3.14.10", "gil_enabled": True, "interpreters": 1}
    )
    result = result.model_copy(update={"backend": "python", "runtime": runtime})
    with pytest.raises(ValueError, match="Python-only"):
        check_correctness(config, result)
