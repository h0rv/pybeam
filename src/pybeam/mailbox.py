from dataclasses import dataclass as plain_dataclass
from typing import final

from pydantic import ConfigDict, PositiveInt
from pydantic.dataclasses import dataclass


@final
@dataclass(frozen=True, slots=True, kw_only=True, config=ConfigDict(strict=True, extra="forbid"))
class BoundedMailbox:
    """Declare limits, without installing runtime admission control.

    Capacity counts queued plus running invocations across all references and
    callers of one destination. max_bytes counts their encoded request payloads,
    not Python or BEAM heap usage, replies, or serialization scratch space.
    Both credits remain held until work actually ends, even after reply timeout
    or caller cancellation. Full admission must raise MailboxOverloaded before
    enqueue, without dropping, retrying, or accumulating waiting callers.
    """

    capacity: PositiveInt
    max_bytes: PositiveInt


@final
@plain_dataclass(frozen=True, slots=True, kw_only=True)
class UnboundedMailbox:
    """Explicitly accept no invocation-count or encoded-payload admission limit."""


type MailboxPolicy = BoundedMailbox | UnboundedMailbox


class MailboxOverloaded(RuntimeError):
    """Admission rejected a request before enqueue; no work was accepted.

    Reserved for an enforcing runtime API. Policy construction does not raise
    this error and existing MVP APIs do not yet enforce these policies.
    """
