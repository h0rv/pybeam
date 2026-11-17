import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from pybeam.mailbox import UnboundedMailbox
from pybeam.supervision.config import (
    NativeChildSpec,
    Restart,
    RestartBudget,
    Shutdown,
    Strategy,
    SupervisorConfig,
)


@pytest.mark.parametrize("count", [-1, -100])
def test_budget_rejects_negative_count(count: int) -> None:
    with pytest.raises(ValueError) as caught:
        RestartBudget(max_restarts=count)


@pytest.mark.parametrize("window", [0, -1])
def test_budget_rejects_nonpositive_window(window: int) -> None:
    with pytest.raises(ValueError) as caught:
        RestartBudget(window_seconds=window)


@pytest.mark.parametrize("grace", [-1, 4294967296])
def test_shutdown_rejects_outside_otp_timer_range(grace: int) -> None:
    with pytest.raises(ValueError) as caught:
        Shutdown(grace)


def test_configuration_is_frozen_and_precise() -> None:
    config = SupervisorConfig(
        mailbox=UnboundedMailbox(),
        max_children=None,
        strategy=Strategy.REST_FOR_ONE,
        budget=RestartBudget(0, 1),
    )
    spec = NativeChildSpec(
        b"service", "native_worker", mailbox=UnboundedMailbox(), restart=Restart.TRANSIENT
    )
    assert config.budget.max_restarts == 0
    assert spec.shutdown.grace_ms == 5000
    with pytest.raises(FrozenInstanceError):
        config.__setattr__("strategy", Strategy.ONE_FOR_ALL)


def test_zero_shutdown_is_an_explicit_force_deadline() -> None:
    assert Shutdown(0).grace_ms == 0


@pytest.mark.parametrize("value", ["True", "False", "'1'", "1.0"])
@pytest.mark.parametrize(
    "constructor,location",
    [
        ("RestartBudget(max_restarts={})", "max_restarts"),
        ("RestartBudget(window_seconds={})", "window_seconds"),
        ("Shutdown(grace_ms={})", "grace_ms"),
        ("SupervisorConfig(mailbox=UnboundedMailbox(), max_children={})", "max_children"),
    ],
)
def test_numeric_configuration__rejects_coercion(
    value: str, constructor: str, location: str
) -> None:
    script = (
        "from pybeam.mailbox import UnboundedMailbox\n"
        "from pybeam.supervision.config import RestartBudget, Shutdown, SupervisorConfig\n"
        "try:\n    "
        + constructor.format(value)
        + "\nexcept ValueError as error:\n    assert {!r} in str(error)\n".format(
            location
        )
        + "else:\n    raise AssertionError('invalid numeric value accepted')\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=10)


def test_imports_do_not_require_native_runtime_or_web_dependencies() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (
        "import sys; sys.path.insert(0, 'src'); "
        "import pybeam.supervision.config; "
        "import pybeam.supervision.ref; "
        "assert 'erlang' not in sys.modules; "
        "assert 'hornbeam' not in sys.modules; "
        "assert 'fastapi' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", script], cwd=root, check=True, timeout=10)
