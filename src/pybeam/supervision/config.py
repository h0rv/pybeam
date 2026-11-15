from dataclasses import dataclass as plain_dataclass
from dataclasses import field
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, NonNegativeInt, PositiveInt, TypeAdapter
from pydantic.dataclasses import dataclass

from pybeam.mailbox import MailboxPolicy

type MaxChildren = Annotated[PositiveInt, Field(strict=True)] | None
_MAX_CHILDREN = TypeAdapter(MaxChildren)


class Strategy(StrEnum):
    ONE_FOR_ONE = "one_for_one"
    ONE_FOR_ALL = "one_for_all"
    REST_FOR_ONE = "rest_for_one"


class Restart(StrEnum):
    PERMANENT = "permanent"
    TRANSIENT = "transient"
    TEMPORARY = "temporary"


@dataclass(frozen=True, slots=True, config=ConfigDict(strict=True, extra="forbid"))
class RestartBudget:
    max_restarts: NonNegativeInt = 3
    window_seconds: PositiveInt = 5


@dataclass(frozen=True, slots=True, config=ConfigDict(strict=True, extra="forbid"))
class Shutdown:
    grace_ms: Annotated[int, Field(ge=0, le=4294967295)] = 5000


@dataclass(frozen=True, slots=True, kw_only=True, config=ConfigDict(strict=True, extra="forbid"))
class SupervisorConfig:
    mailbox: MailboxPolicy
    max_children: MaxChildren
    strategy: Strategy = Strategy.ONE_FOR_ONE
    budget: RestartBudget = field(default_factory=RestartBudget)


def validate_max_children(value: int | None) -> None:
    _MAX_CHILDREN.validate_python(value)


@plain_dataclass(frozen=True, slots=True)
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
