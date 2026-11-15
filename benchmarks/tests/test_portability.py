import hashlib
from pathlib import Path

import pytest

from benchmarks.contract import Config, StrictModel

ROOT = Path(__file__).resolve().parents[2]


class ArchivedFile(StrictModel):
    path: str
    bytes: int
    sha256: str


class ArchiveManifest(StrictModel):
    source_directory: str
    purpose: str
    files: tuple[ArchivedFile, ...]


@pytest.mark.parametrize(
    "name",
    [
        "examples/python_reference.json",
        "examples/serial_python.json",
        "examples/serial_pybeam.json",
        "examples/serial_erlang.json",
        "native_reference/configs/default.json",
        "native_reference/configs/small.json",
        "pybeam_reference_smoke.json",
    ],
)
def test_shipped_configuration__uses_this_checkout_and_private_native_assets(name: str) -> None:
    config = Config.model_validate_json((ROOT / "benchmarks" / name).read_text())
    assert (ROOT / config.python_source).resolve() == ROOT
    if config.backend == "pybeam":
        assert config.native_source == "../native-benchmark-current"
    else:
        assert config.native_source is None
    assert config.adapter_command[0] == "benchmarks/.venv/bin/python"
    assert (ROOT / config.adapter_command[0]).is_file()


def test_archived_evidence__retains_all_original_file_hashes_without_source_worktree() -> None:
    directory = ROOT / "benchmarks/evidence/native-reference-archive"
    manifest = ArchiveManifest.model_validate_json((directory / "manifest.json").read_text())
    assert len(manifest.files) == 45
    assert len({entry.path for entry in manifest.files}) == 45
    for entry in manifest.files:
        path = (directory / entry.path).resolve()
        assert path.is_relative_to(directory)
        assert path.stat().st_size == entry.bytes
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry.sha256
