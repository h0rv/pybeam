from dataclasses import FrozenInstanceError
from dataclasses import replace
from pathlib import Path
from subprocess import run
from sys import executable

import pytest

from pybeam.mailbox import BoundedMailbox, MailboxOverloaded, UnboundedMailbox
from tests.typechecking import TYPECHECK_CONFIG

ROOT = Path(__file__).resolve().parents[1]
IMPORTS = "\n".join(
    (
        "from typing import assert_never",
        "from pybeam.mailbox import (",
        "    BoundedMailbox, UnboundedMailbox, MailboxPolicy, MailboxOverloaded,",
        ")",
        "",
    )
)


def test_policy__requires_explicit_positive_limits() -> None:
    policy = BoundedMailbox(capacity=128, max_bytes=4096)
    assert policy.capacity == 128
    assert policy.max_bytes == 4096
    assert replace(policy, capacity=1) == BoundedMailbox(capacity=1, max_bytes=4096)
    assert UnboundedMailbox() == UnboundedMailbox()
    assert issubclass(MailboxOverloaded, RuntimeError)


@pytest.mark.parametrize("capacity,max_bytes", [(0, 1), (-1, 1), (1, 0), (1, -1)])
def test_policy__rejects_nonpositive_limits(capacity: int, max_bytes: int) -> None:
    with pytest.raises(ValueError) as caught:
        BoundedMailbox(capacity=capacity, max_bytes=max_bytes)


@pytest.mark.parametrize("field", ["capacity", "max_bytes"])
@pytest.mark.parametrize("value", ["True", "False", "1.0", "'128'", "None"])
def test_policy__rejects_untyped_invalid_values(field: str, value: str) -> None:
    arguments = {"capacity": "128", "max_bytes": "4096", field: value}
    script = (
        IMPORTS
        + "try:\n    BoundedMailbox(capacity={}, max_bytes={})\n".format(
            arguments["capacity"], arguments["max_bytes"]
        )
        + "except ValueError as error:\n"
        + "    assert {!r} in str(error)\n".format(field)
        + "else:\n    raise AssertionError('invalid value accepted')"
    )
    result = run([executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("constructor", ["BoundedMailbox()", "BoundedMailbox(capacity=128)"])
def test_policy__has_no_implicit_limits(constructor: str) -> None:
    result = run(
        [executable, "-c", IMPORTS + constructor], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "TypeError:" in result.stderr
    assert "missing" in result.stderr


@pytest.mark.parametrize("mutation", ["del policy.capacity", "policy.capacity = 2"])
def test_policy__is_immutable(mutation: str) -> None:
    script = IMPORTS + "policy = BoundedMailbox(capacity=1, max_bytes=1)\n" + mutation
    result = run([executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "FrozenInstanceError:" in result.stderr


def test_policy__imports_without_optional_dependencies() -> None:
    script = "\n".join(
        (
            "import sys",
            "sys.path.insert(0, 'src')",
            IMPORTS,
            "assert BoundedMailbox(capacity=128, max_bytes=4096).capacity == 128",
            "assert not {'hornbeam', 'erlang', 'fastapi', 'litestar', 'pyview'} & sys.modules.keys()",
        )
    )
    result = run([executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def check_types(tmp_path: Path, code: str) -> tuple[int, str]:
    probe = tmp_path / "probe.py"
    probe.write_text(IMPORTS + code)
    result = run(
        [
            str(Path(executable).parent / "ty"),
            "check",
            *TYPECHECK_CONFIG,
            "--project",
            str(ROOT),
            "--error-on-warning",
            "--error",
            "all",
            str(probe),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def test_policy__typechecks_exhaustively(tmp_path: Path) -> None:
    code = "\n".join(
        (
            "def describe(policy: MailboxPolicy) -> int:",
            "    match policy:",
            "        case BoundedMailbox(capacity=count):",
            "            return count",
            "        case UnboundedMailbox():",
            "            return 0",
            "        case _:",
            "            assert_never(policy)",
            "describe(BoundedMailbox(capacity=128, max_bytes=4096))",
            "describe(UnboundedMailbox())",
        )
    )
    status, output = check_types(tmp_path, code)
    assert status == 0, output


@pytest.mark.parametrize(
    "code,diagnostic",
    [
        ("BoundedMailbox()", "missing-argument"),
        ("BoundedMailbox(capacity=128)", "missing-argument"),
        ("BoundedMailbox(128, 4096)", "too-many-positional-arguments"),
        ('BoundedMailbox(capacity="128", max_bytes=4096)', "invalid-argument-type"),
        ('BoundedMailbox(capacity=128, max_bytes="4096")', "invalid-argument-type"),
        ("BoundedMailbox(capacity=128, max_bytes=4096, block=True)", "unknown-argument"),
        ("UnboundedMailbox(capacity=128)", "unknown-argument"),
        ("UnboundedMailbox(bounded=False)", "unknown-argument"),
        ("policy: MailboxPolicy = None", "invalid-assignment"),
        ("policy: MailboxPolicy = True", "invalid-assignment"),
        ("class Other(BoundedMailbox): pass", "subclass-of-final-class"),
        ("class Other(UnboundedMailbox): pass", "subclass-of-final-class"),
        (
            "policy = BoundedMailbox(capacity=128, max_bytes=4096)\npolicy.capacity = 1",
            "invalid-assignment",
        ),
    ],
)
def test_policy__rejects_invalid_static_contracts(
    tmp_path: Path, code: str, diagnostic: str
) -> None:
    status, output = check_types(tmp_path, code)
    assert status == 1, output
    assert diagnostic in output
