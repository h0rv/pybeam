import subprocess
from pathlib import Path

import pytest

from tests.native.runtime import NATIVE, ROOT, require_runtime

pytestmark = pytest.mark.runtime


@pytest.fixture
def native_runtime(request: pytest.FixtureRequest) -> Path:
    return require_runtime(request)


@pytest.fixture
def native_tests(native_runtime: Path, tmp_path: Path) -> Path:
    assert (native_runtime / "ebin" / "py_supervision.beam").is_file()
    source = NATIVE / "test/py_supervision_tests.erl"
    subprocess.run(["erlc", "-o", str(tmp_path), str(source)], check=True, timeout=20)
    return tmp_path


def run_native(native_tests: Path, native_runtime: Path, expression: str) -> None:
    log = native_tests / "runtime.log"
    with log.open("wb") as output:
        result = subprocess.run(
            [
                "erl",
                "-noshell",
                "-pa",
                str(native_runtime / "ebin"),
                str(native_tests),
                "-eval",
                expression,
            ],
            cwd=native_tests,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
    assert result.returncode == 0, log.read_text()


def test_otp_restart_and_shutdown_contracts(native_tests: Path, native_runtime: Path) -> None:
    run_native(
        native_tests,
        native_runtime,
        "case eunit:test(py_supervision_tests, [verbose]) of ok -> halt(0); _ -> halt(1) end.",
    )


def test_python_api_and_process_bound_state_reset(native_tests: Path, native_runtime: Path) -> None:
    source = str(ROOT / "src")
    tests = str(ROOT / "tests/supervision")
    run_native(
        native_tests,
        native_runtime,
        'ok = py_supervision_tests:runtime("{}", "{}"), halt().'.format(source, tests),
    )
