import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT.parent / "erlang-python" / "priv"
pytestmark = pytest.mark.skipif(
    not (NATIVE / "erlang_services.py").is_file(),
    reason="requires sibling erlang-python Python service types",
)


def check(source: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "ty"),
            "check",
            "--error-on-warning",
            "--extra-search-path",
            str(ROOT / "src"),
            "--extra-search-path",
            str(NATIVE),
            str(source),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_services__paramspec_and_none_return_are_checked(tmp_path: Path) -> None:
    source = tmp_path / "caller.py"
    declarations = """
from typing import assert_type
from pybeam.services.background import ServiceRef, ServiceSlot, ServiceState

async def observe(ref: ServiceRef) -> None:
    assert_type(await ref.wait(), None)
    assert_type(await ref.stop(), None)
    assert_type(ref.state, ServiceState)

async def consume(topic: str, *, capacity: int) -> None:
    pass

async def wrong_return() -> int:
    return 1

slot = ServiceSlot(capacity=1)
"""
    source.write_text(
        declarations + "assert_type(slot.start(consume, 'events', capacity=8), ServiceRef)\n"
    )
    valid = check(source)
    assert valid.returncode == 0, valid.stdout + valid.stderr
    for call in (
        "ServiceSlot()",
        "ServiceSlot(capacity=2)",
        "slot.start(consume, 42, capacity=8)",
        "slot.start(consume, 'events', capacity='eight')",
        "slot.start(consume, 'events')",
        "slot.start(consume, 'events', capacity=8, unknown=True)",
        "slot.start(wrong_return)",
    ):
        source.write_text(declarations + call + "\n")
        invalid = check(source)
        assert invalid.returncode != 0, call
        assert "error[" in invalid.stdout, invalid.stdout + invalid.stderr
        assert "unresolved-import" not in invalid.stdout, invalid.stdout
