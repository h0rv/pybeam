import re
from dataclasses import dataclass
from typing import Literal

from examples.distributed.jobs import JobResult
from pybeam.codec import Json
from pybeam.distribution import HandlerIdentity, Reply
from pybeam.runtime.cluster import NodeExit
from pybeam.runtime.ownership import Identity, Registry


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoProof:
    epmd: Identity
    vm_a: Identity
    vm_b: Identity
    restarted_b: Identity
    first: Reply
    down: Reply
    absent: Reply
    fresh: Reply
    inflight: Reply
    inflight_loss: Reply
    controls: Reply
    stale_ack: Reply
    second: Reply
    cleanup: Registry
    shutdowns: tuple[NodeExit, ...]


def safe_handler(value: HandlerIdentity) -> HandlerIdentity:
    if (
        re.fullmatch(r"pybeam_[ab]_[0-9a-f]{8}@127\.0\.0\.1", value.node) is None
        or re.fullmatch(r"<[0-9]{1,10}\.[0-9]{1,10}\.[0-9]{1,10}>", value.pid) is None
        or re.fullmatch(r"[0-9A-F]{32}", value.incarnation) is None
        or (
            value.message_id is not None
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value.message_id) is None
        )
    ):
        raise ValueError("Handler identity is outside the sanitized local walkthrough format")
    return value


def task_pid(value: str) -> str:
    if re.fullmatch(r"<[0-9]{1,10}\.[0-9]{1,10}\.[0-9]{1,10}>", value) is None:
        raise ValueError("Task PID is outside the local walkthrough format")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeObserved:
    kind: Literal["node"] = "node"
    alias: Literal["a", "b"]
    birth: Identity
    handler: HandlerIdentity
    status: Literal["observed", "fresh"]

    def __post_init__(self) -> None:
        safe_handler(self.handler)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExchangeObserved:
    kind: Literal["exchange"] = "exchange"
    sender: HandlerIdentity
    recipient: HandlerIdentity
    receipt: HandlerIdentity
    reverse_ack: HandlerIdentity
    worker: str
    worker_dead: Literal[True]
    result: JobResult

    def __post_init__(self) -> None:
        for handler in (self.sender, self.recipient, self.receipt, self.reverse_ack):
            safe_handler(handler)
        task_pid(self.worker)
        if self.receipt.message_id is None or self.reverse_ack.message_id is None:
            raise ValueError("Missing handler-derived message identity")
        if (self.receipt.node, self.receipt.pid, self.receipt.incarnation) != (
            self.recipient.node,
            self.recipient.pid,
            self.recipient.incarnation,
        ) or (self.reverse_ack.node, self.reverse_ack.pid, self.reverse_ack.incarnation) != (
            self.sender.node,
            self.sender.pid,
            self.sender.incarnation,
        ):
            raise ValueError("Receipt and reverse ACK do not match observed handlers")
        if self.reverse_ack.message_id != self.receipt.message_id:
            raise ValueError("Reverse ACK differs from the observed message")
        if self.worker == self.recipient.pid or self.result != JobResult(
            value=42, side_effect_count=1, python_version="3.14.10"
        ):
            raise ValueError("Result is not an independently completed temporary Task")


def exchange(reply: Reply) -> ExchangeObserved:
    ack = reply.reverse_ack
    if (
        reply.status != "ok"
        or reply.identity is None
        or reply.recipient is None
        or reply.receipt is None
        or ack is None
        or ack.status != "ok"
        or ack.receipt is None
        or reply.worker is None
        or reply.worker_kind != "temporary_otp_task"
        or not reply.worker_dead
        or reply.result is None
    ):
        raise ValueError("Incomplete observed exchange")
    return ExchangeObserved(
        sender=reply.identity,
        recipient=reply.recipient,
        receipt=reply.receipt,
        reverse_ack=ack.receipt,
        worker=reply.worker,
        worker_dead=True,
        result=Json(JobResult).decode(reply.result),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class StateObserved:
    kind: Literal["state"] = "state"
    state: Literal["task_pending", "nodedown_uncertain", "fresh_empty", "stale_ack_rejected"]
    handler: HandlerIdentity | None = None
    worker: str | None = None

    def __post_init__(self) -> None:
        if self.handler is not None:
            safe_handler(self.handler)
        if self.worker is not None:
            task_pid(self.worker)


@dataclass(frozen=True, slots=True, kw_only=True)
class CleanupObserved:
    kind: Literal["cleanup"] = "cleanup"
    state: Literal["confirmed", "uncertain"]
    owned: tuple[Identity, ...]


type Observation = NodeObserved | ExchangeObserved | StateObserved | CleanupObserved
OBSERVATION = Json(Observation)


@dataclass(frozen=True, slots=True, kw_only=True)
class WalkthroughProof:
    kind: Literal["walkthrough"] = "walkthrough"
    observations: tuple[Observation, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.observations) <= 16:
            raise ValueError("Walkthrough must have 1 to 16 observations")
        births = tuple(item for item in self.observations if isinstance(item, NodeObserved))
        exchanges = tuple(item for item in self.observations if isinstance(item, ExchangeObserved))
        states = tuple(item.state for item in self.observations if isinstance(item, StateObserved))
        cleanup = self.observations[-1]
        if (
            tuple(item.alias for item in births) != ("a", "b", "b")
            or len({item.birth for item in births}) != 3
            or births[1].handler.incarnation == births[2].handler.incarnation
            or len(exchanges) != 2
            or states != ("task_pending", "nodedown_uncertain", "fresh_empty", "stale_ack_rejected")
            or not isinstance(cleanup, CleanupObserved)
            or cleanup.state != "confirmed"
        ):
            raise ValueError("Incomplete finite walkthrough evidence")
        if (
            exchanges[0].sender != births[0].handler
            or exchanges[1].sender != births[0].handler
            or not {item.birth for item in births}.issubset(cleanup.owned)
            or exchanges[0].recipient != births[1].handler
            or exchanges[1].recipient != births[2].handler
        ):
            raise ValueError("Exchange recipient differs from observed node incarnation")


def sanitized(proof: DemoProof) -> WalkthroughProof:
    if (
        proof.first.identity is None
        or proof.first.recipient is None
        or proof.fresh.identity is None
    ):
        raise ValueError("Missing observed handler identities")
    if (
        proof.inflight.status != "pending"
        or proof.inflight.worker is None
        or proof.inflight_loss.status != "uncertain"
        or proof.absent.status != "uncertain"
        or proof.absent.receipt is not None
        or proof.stale_ack.status != "stale"
        or proof.fresh.count != 0
        or proof.fresh.completed != 0
        or not proof.down.events
        or proof.down.events[-1].event != "nodedown"
        or proof.down.events[-1].node != proof.first.recipient.node
        or proof.first.recipient.node in proof.down.nodes
    ):
        raise ValueError("Missing uncertainty, fresh state or stale ACK evidence")
    return WalkthroughProof(
        observations=(
            NodeObserved(
                alias="a", birth=proof.vm_a, handler=proof.first.identity, status="observed"
            ),
            NodeObserved(
                alias="b", birth=proof.vm_b, handler=proof.first.recipient, status="observed"
            ),
            exchange(proof.first),
            StateObserved(
                state="task_pending", handler=proof.inflight.identity, worker=proof.inflight.worker
            ),
            StateObserved(state="nodedown_uncertain", handler=proof.down.identity),
            NodeObserved(
                alias="b", birth=proof.restarted_b, handler=proof.fresh.identity, status="fresh"
            ),
            StateObserved(state="fresh_empty", handler=proof.fresh.identity),
            StateObserved(state="stale_ack_rejected", handler=proof.stale_ack.identity),
            exchange(proof.second),
            CleanupObserved(
                state="confirmed" if proof.cleanup.phase == "confirmed" else "uncertain",
                owned=proof.cleanup.owned,
            ),
        )
    )
