import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_default_collection__does_not_import_optional_frameworks() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pytest, sys\n"
            "status = pytest.main(['tests', '--collect-only', '-q', '-m', 'not runtime and not browser'])\n"
            "assert status == 0\n"
            "assert not {'hornbeam', 'fastapi', 'litestar', 'pyview'} & sys.modules.keys()\n",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_explicit_framework_file__requires_opt_in_before_import() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_fastapi.py", "--collect-only", "-q"],
        cwd=ROOT,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "require --run-frameworks" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
