import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pybeam.actors.native import UnboundedMailbox
from pybeam.mailbox import UnboundedMailbox as SharedUnboundedMailbox
from tests.test_actors_typing import checker
from tests.typechecking import TYPECHECK_CONFIG


def test_unbounded_policy__uses_the_shared_policy_type() -> None:
    assert UnboundedMailbox is SharedUnboundedMailbox


def test_public_actor_types__preserve_concrete_call_arguments_and_returns() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [checker(), "check", *TYPECHECK_CONFIG, "tests/actor_public_positive.py"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_public_actor_types__reject_invalid_calls_and_unsupported_policies(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "tests/actor_public_negative.txt").read_text()
    probe = tmp_path / "actor_public_negative.py"
    probe.write_text(source)
    result = subprocess.run(
        [
            checker(),
            "check",
            *TYPECHECK_CONFIG,
            "--project",
            str(root),
            "--output-format",
            "concise",
            str(probe),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    rejected = {int(match[1]) for match in re.finditer(r":(\d+):\d+: error\[", output)}
    for line, text in enumerate(source.splitlines(), start=1):
        if "# rejected" in text:
            assert line in rejected, output


@pytest.mark.runtime
def test_public_actor_api__preserves_objects_and_owns_native_lifecycle(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and a built runtime with Python actor bindings")
    configured: object = request.config.getoption("hornbeam_runtime")
    if not isinstance(configured, str):
        raise TypeError("Runtime path must be text")
    runtime = Path(configured).resolve()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(runtime) + '"\n'
    )
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    result = subprocess.run(
        [sys.executable, str(root / "tests/native/actor_public.py")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PUBLIC_ACTORS_OK" in result.stdout
