from pathlib import Path

import pytest

from tests.native.runtime import require_runtime, run_launcher


@pytest.mark.runtime
def test_services_fixture__native_result_and_cleanup(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    runtime = require_runtime(request)
    result = run_launcher(runtime, "services.escript", tmp_path)
    (tmp_path / "services-example.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ServiceSlot bounds local tasks only" in result.stdout
    assert (
        'services-example-ok{<<"completed">>,<<"failed">>,<<"cancelled">>,<<"x">>,true,4,0}'
        in "".join(result.stdout.split())
    ), result.stdout
    assert "cleanup is not acknowledged" not in result.stderr
    unsupported = run_launcher(runtime, "services.escript", tmp_path, "--bounded-mailbox")
    assert unsupported.returncode == 2, unsupported.stdout + unsupported.stderr
    assert "Native bounded mailbox admission is unavailable" in unsupported.stderr
    assert "services-example-ok" not in unsupported.stdout
