from pathlib import Path

import pytest

from tests.native.runtime import require_runtime, run_launcher, run_suites


@pytest.mark.runtime
def test_actor_runtime__checks_local_references_and_actor_processes(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    runtime = require_runtime(request)
    output = run_suites(
        runtime,
        ("py_actor_contract_SUITE", "py_actor_SUITE", "py_local_object_SUITE"),
        tmp_path,
    )
    assert "NATIVE_CT_RESULT {20,0,{0,0}}" in output


@pytest.mark.runtime
def test_actor_fixture__native_entry_checks_state_and_cleanup(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    runtime = require_runtime(request)
    result = run_launcher(runtime, "actors.escript", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert "Internal native actor regression fixture." in lines
    assert "Mailbox policy: explicit unbounded. No mailbox bounds." in lines
    assert "Concurrent results: (1, 2, 3, 4, 5, 6, 7, 8)" in lines
    assert "First actor: value=8, label=visits" in lines
    assert "Second actor: value=2, label=other" in lines
    assert "Cleanup: both actors stopped, state collected, context stopped." in lines
