import shutil
from pathlib import Path

import pytest

from tests.native.runtime import NATIVE, require_runtime, run_suites


@pytest.mark.runtime
def test_admission__retains_native_capacity_until_execution_ends(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    runtime = require_runtime(request)
    shutil.copyfile(NATIVE / "test" / "py_test_admission.py", tmp_path / "py_test_admission.py")
    output = run_suites(runtime, ("py_work_admission_SUITE",), tmp_path)
    assert "NATIVE_CT_RESULT {20,0,{0,0}}" in output
