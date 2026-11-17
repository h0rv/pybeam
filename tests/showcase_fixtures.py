from dataclasses import replace

from examples.distributed.jobs import JobResult
from examples.distributed.models import DemoProof, WalkthroughProof, sanitized
from pybeam.codec import Json
from pybeam.distribution import HandlerIdentity, NodeEvent, Reply
from pybeam.runtime.ownership import Identity, Registry


def controlled_proof() -> DemoProof:
    a = HandlerIdentity(node="pybeam_a_01234567@127.0.0.1", pid="<0.10.0>", incarnation="A" * 32)
    b = HandlerIdentity(node="pybeam_b_01234567@127.0.0.1", pid="<0.20.0>", incarnation="B" * 32)
    fresh = replace(b, incarnation="C" * 32)
    epmd = Identity(pid=101, created=1.0)
    births = tuple(Identity(pid=pid, created=2.0) for pid in (201, 202, 203))

    def receipt(recipient: HandlerIdentity, message: str) -> Reply:
        private_data = "SECRET_RESERVATION"
        return Reply(
            status="ok",
            identity=a,
            recipient=recipient,
            receipt=replace(recipient, message_id=message),
            reverse_ack=Reply(status="ok", receipt=replace(a, message_id=message)),
            worker="<0.99.0>",
            worker_kind="temporary_otp_task",
            worker_dead=True,
            result=Json(JobResult)
            .encode(JobResult(value=42, side_effect_count=1, python_version="3.14.10"))
            .decode(),
            token=private_data,
            payload="SECRET_PAYLOAD",
        )

    return DemoProof(
        epmd=epmd,
        vm_a=births[0],
        vm_b=births[1],
        restarted_b=births[2],
        first=receipt(b, "first"),
        second=receipt(fresh, "second"),
        down=Reply(status="ok", identity=a, events=(NodeEvent(event="nodedown", node=b.node),)),
        absent=Reply(status="uncertain"),
        fresh=Reply(status="ok", identity=fresh),
        inflight=Reply(status="pending", identity=b, worker="<0.98.0>"),
        inflight_loss=Reply(status="uncertain"),
        controls=Reply(status="ok"),
        stale_ack=Reply(status="stale"),
        cleanup=Registry(phase="confirmed", owned=(epmd, *births)),
        shutdowns=(),
    )


def controlled_walkthrough() -> WalkthroughProof:
    return sanitized(controlled_proof())
