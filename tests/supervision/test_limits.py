from typing import Literal, assert_never

import pytest

from pybeam.mailbox import BoundedMailbox, MailboxPolicy, UnboundedMailbox
from pybeam.supervision.config import NativeChildSpec, SupervisorConfig
from pybeam.supervision.errors import CapabilityUnavailable
from pybeam.supervision.ref import SupervisorRef

type Operation = Literal["start", "subtree", "inspect", "stop", "restart", "delete"]


async def operate(ref: SupervisorRef, operation: Operation) -> None:
    match operation:
        case "start":
            await ref.start_child(NativeChildSpec(b"worker", "unused", mailbox=UnboundedMailbox()))
        case "subtree":
            await ref.start_supervisor(
                b"subtree", SupervisorConfig(mailbox=UnboundedMailbox(), max_children=None)
            )
        case "inspect":
            await ref.children()
        case "stop":
            await ref.stop_child(b"worker")
        case "restart":
            await ref.restart_child(b"worker")
        case "delete":
            await ref.delete_child(b"worker")
        case _:
            assert_never(operation)


@pytest.mark.parametrize("operation", ["start", "subtree", "inspect", "stop", "restart", "delete"])
@pytest.mark.parametrize(
    ("policy", "max_children"),
    [
        (BoundedMailbox(capacity=128, max_bytes=65536), None),
        (UnboundedMailbox(), 128),
    ],
)
async def test_control_limits_fail_before_import_or_native_enqueue(
    operation: Operation, policy: MailboxPolicy, max_children: int | None
) -> None:
    ref = SupervisorRef.attach(object(), mailbox=policy, max_children=max_children)
    with pytest.raises(CapabilityUnavailable, match="native"):
        await operate(ref, operation)


async def test_worker_policy_is_separate_and_fails_closed() -> None:
    ref = SupervisorRef.attach(object(), mailbox=UnboundedMailbox(), max_children=None)
    spec = NativeChildSpec(
        b"worker", "unused", mailbox=BoundedMailbox(capacity=128, max_bytes=65536)
    )
    with pytest.raises(CapabilityUnavailable, match="native"):
        await ref.start_child(spec)


async def test_subtree_policy_is_separate_and_fails_closed() -> None:
    ref = SupervisorRef.attach(object(), mailbox=UnboundedMailbox(), max_children=None)
    config = SupervisorConfig(
        mailbox=BoundedMailbox(capacity=128, max_bytes=65536), max_children=None
    )
    with pytest.raises(CapabilityUnavailable, match="native"):
        await ref.start_supervisor(b"subtree", config)


async def test_finite_child_limit_does_not_silently_create_unbounded_subtree() -> None:
    ref = SupervisorRef.attach(object(), mailbox=UnboundedMailbox(), max_children=None)
    config = SupervisorConfig(mailbox=UnboundedMailbox(), max_children=128)
    with pytest.raises(CapabilityUnavailable, match="atomic native"):
        await ref.start_supervisor(b"subtree", config)


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_child_counts_are_rejected(limit: int) -> None:
    with pytest.raises(ValueError):
        SupervisorConfig(mailbox=UnboundedMailbox(), max_children=limit)
    with pytest.raises(ValueError):
        SupervisorRef.attach(object(), mailbox=UnboundedMailbox(), max_children=limit)
