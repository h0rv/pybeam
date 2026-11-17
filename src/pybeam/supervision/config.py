from dataclasses import dataclass, field
from enum import StrEnum

from pybeam.mailbox import MailboxPolicy


def _require(value: int, name: str, low: int = 1, high: int | None = None) -> None:
    if type(value) is not int or value < low or (high is not None and value > high):
        bound = "at least " + str(low) if high is None else "from " + str(low) + " to " + str(high)
        raise ValueError(name + " must be an integer " + bound)


class Strategy(StrEnum):
    ONE_FOR_ONE = "one_for_one"
    ONE_FOR_ALL = "one_for_all"
    REST_FOR_ONE = "rest_for_one"


class Restart(StrEnum):
    PERMANENT = "permanent"
    TRANSIENT = "transient"
    TEMPORARY = "temporary"


@dataclass(frozen=True, slots=True)
class RestartBudget:
    max_restarts: int = 3
    window_seconds: int = 5

    def __post_init__(self) -> None:
        _require(self.max_restarts, "max_restarts", 0)
        _require(self.window_seconds, "window_seconds")


@dataclass(frozen=True, slots=True)
class Shutdown:
    grace_ms: int = 5000

    def __post_init__(self) -> None:
        _require(self.grace_ms, "grace_ms", 0, 4294967295)


@dataclass(frozen=True, slots=True, kw_only=True)
class SupervisorConfig:
    mailbox: MailboxPolicy
    max_children: int | None
    strategy: Strategy = Strategy.ONE_FOR_ONE
    budget: RestartBudget = field(default_factory=RestartBudget)

    def __post_init__(self) -> None:
        validate_max_children(self.max_children)
        if not isinstance(self.strategy, Strategy) or not isinstance(self.budget, RestartBudget):
            raise TypeError("strategy and budget must be Strategy and RestartBudget values")


def validate_max_children(value: int | None) -> None:
    if value is not None:
        _require(value, "max_children")


@dataclass(frozen=True, slots=True)
class NativeChildSpec:
    """The MFA must start and link one OTP worker, not an asyncio task.

    The worker owns startup rollback, Python environments, and task cleanup.
    Arguments must be supported by erlang-python's native term conversion.
    """

    child_id: bytes
    module: str
    mailbox: MailboxPolicy = field(kw_only=True)
    function: str = "start_link"
    args: tuple[object, ...] = ()
    restart: Restart = Restart.PERMANENT
    shutdown: Shutdown = field(default_factory=Shutdown)
