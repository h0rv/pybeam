import re
from pathlib import Path

import pytest

from tests.native.runtime import require_runtime, run_launcher

pytestmark = pytest.mark.runtime


@pytest.mark.parametrize("attempt", range(2))
def test_native_fixture_confirms_restart_and_shutdown(
    request: pytest.FixtureRequest, tmp_path: Path, attempt: int
) -> None:
    runtime = require_runtime(request)
    result = run_launcher(runtime, "supervision.escript", tmp_path)
    output_text = result.stdout + result.stderr
    (tmp_path / "supervision-{}.log".format(attempt)).write_text(output_text)
    assert result.returncode == 0, output_text
    assert "native OTP demo counter" in output_text
    assert "bounded control unavailable: native admission gate is not connected" in output_text
    assert "incremented state=1" in output_text
    restarted = re.search(r"restart old_pid=(.*?) new_pid=(.*?) state=0", output_text)
    assert restarted is not None, output_text
    old_pid, new_pid = restarted.groups()
    assert old_pid != new_pid
    assert "started pid={} state=0".format(old_pid) in output_text
    assert "worker shutdown reason=shutdown" in output_text
    assert "shutdown children=0" in output_text
    assert "root shutdown confirmed" in output_text
