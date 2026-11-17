from dataclasses import dataclass
from typing import final


def _require(value: int, name: str, low: int = 1, high: int | None = None) -> None:
    if type(value) is not int or value < low or (high is not None and value > high):
        bound = "at least " + str(low) if high is None else "from " + str(low) + " to " + str(high)
        raise ValueError(name + " must be an integer " + bound)


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class BoundedMailbox:
    """Declare limits, without installing runtime admission control.

    Capacity counts queued plus running invocations across all references and
    callers of one destination. max_bytes counts their encoded request payloads,
    not Python or BEAM heap usage, replies, or serialization scratch space.
    Both credits remain held until work actually ends, even after reply timeout
    or caller cancellation. Full admission must raise MailboxOverloaded before
    enqueue, without dropping, retrying, or accumulating waiting callers.
    """

    capacity: int
    max_bytes: int

    def __post_init__(self) -> None:
        _require(self.capacity, "capacity")
        _require(self.max_bytes, "max_bytes")


@final
@dataclass(frozen=True, slots=True, kw_only=True)
class UnboundedMailbox:
    """Explicitly accept no invocation-count or encoded-payload admission limit."""


type MailboxPolicy = BoundedMailbox | UnboundedMailbox


class MailboxOverloaded(RuntimeError):
    """Admission rejected a request before enqueue; no work was accepted.

    Reserved for an enforcing runtime API. Policy construction does not raise
    this error and existing MVP APIs do not yet enforce these policies.
    """
