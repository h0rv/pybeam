from pathlib import Path

import pytest

from tests.native.task_helpers import run_native

pytestmark = pytest.mark.runtime


def test_soul__http_survives_inspection_stop_and_replacement(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    if not request.config.getoption("run_runtime") or not request.config.getoption(
        "run_frameworks"
    ):
        pytest.skip("Requires --run-runtime --run-frameworks and the paired runtime")
    result = run_native(
        tmp_path,
        "    from examples.soul.main import main as walkthrough\n    await walkthrough()\n",
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Before the bug:" in output
    assert "Sum(-1) does not yield:" in output
    assert "in iterative_sum" in output
    assert "Stopped child; task outcome ('signal', 9)" in output
    assert "Replacement sum(1,000,000,000) = 500000000500000000" in output
    assert "After replacing the entry for new calculations:" in output
    assert "VM PID unchanged" in output
    assert "Traceback (most recent call last)" not in output
