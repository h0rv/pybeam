import subprocess
from pathlib import Path

import pytest

from examples.distributed.config import configuration
from pybeam.runtime.assets import discover_assets
from pybeam.runtime.process import _stop_owned

pytestmark = pytest.mark.runtime


def run_owned(argv: list[str]) -> tuple[int, bytes, bytes]:
    process = subprocess.Popen(
        argv, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        stdout, stderr = process.communicate(timeout=20)
        return process.wait(timeout=1), stdout, stderr
    finally:
        if process.poll() is None:
            _stop_owned(process, 1.0)


def test_distribution__recipient_admission_expiry_and_stale_state_controls(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires explicit --run-runtime and the private paired native build")
    config, _ = configuration()
    assert config.development_runtime is not None
    source = config.development_runtime.resolve().parents[1] / "test" / "py_distribution_tests.erl"
    status, stdout, stderr = run_owned(["erlc", "-o", str(tmp_path), str(source)])
    assert status == 0, stdout + stderr
    assets = discover_assets(config)
    argv = [
        str(assets.executable),
        "+S",
        "1:1",
        "-noshell",
        "-noinput",
        "-pa",
        str(tmp_path),
        *(str(path) for path in assets.code_paths),
        "-eval",
        "R=eunit:test(py_distribution_tests,[verbose]),case R of ok->halt(0);_->halt(1) end.",
    ]
    status, stdout, stderr = run_owned(argv)
    assert status == 0, stdout + stderr
    assert b"All 4 tests passed" in stdout
