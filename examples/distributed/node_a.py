import asyncio
import os
from pathlib import Path

import psutil

from examples.distributed.jobs import JobRequest
from examples.distributed.probes import controls
from pybeam import run
from pybeam.distribution import HandlerIdentity, Reply, Request, local, remote, reservation
from pybeam.runtime.ownership import durable_write, identity


async def sample(directory: Path, stage: str) -> None:
    peer = await remote("b", Request(op="snapshot"))
    own = await local(Request(op="snapshot"))
    if peer.status != "ok" or peer.identity is None or own.identity is None:
        raise RuntimeError("Explicit join did not reach both real nodes")
    if own.definitions or peer.definitions != ("double",):
        raise RuntimeError("The named task definition must be registered only by B")
    if peer.identity.node not in own.nodes or own.identity.node not in peer.nodes:
        raise RuntimeError("Mutual OTP nodes() membership missing")
    receipt = await remote("b", Request(op="receipt", id=stage, ack_alias="a"))
    if receipt.status != "ok" or receipt.receipt != peer.identity.model_copy(
        update={"message_id": stage}
    ):
        raise RuntimeError("Receipt was not produced by the recipient handler")
    ack = receipt.reverse_ack
    if (
        ack is None
        or ack.status != "ok"
        or ack.receipt is None
        or ack.receipt.node != own.identity.node
    ):
        raise RuntimeError("B did not receive the reverse OTP acknowledgment")
    payload = JobRequest(value=21, delay_ms=100).model_dump_json()
    reserved = await remote("b", reservation(peer.identity.incarnation, stage, payload))
    if reserved.status != "ok" or reserved.token is None:
        raise RuntimeError("Recipient reservation failed")
    token = reserved.token
    submitted = await remote(
        "b",
        Request(op="submit", incarnation=peer.identity.incarnation, token=token, payload=payload),
    )
    if submitted.status != "ok":
        raise RuntimeError("Submission is uncertain; automatic replay is forbidden")
    async with asyncio.timeout(5):
        while True:
            result = await remote(
                "b", Request(op="result", incarnation=peer.identity.incarnation, token=token)
            )
            if result.status == "ok":
                break
            if result.status != "pending":
                raise RuntimeError("Observation is uncertain; automatic replay is forbidden")
            await asyncio.sleep(0.02)
    if (
        result.identity != peer.identity
        or result.worker_kind != "temporary_otp_task"
        or not result.worker_dead
    ):
        raise RuntimeError("Result did not come from the recipient's acknowledged OTP task")
    durable_write(
        directory / (stage + ".json"),
        Reply(
            status="ok",
            identity=own.identity,
            recipient=result.identity,
            receipt=receipt.receipt,
            reverse_ack=ack,
            worker=result.worker,
            worker_kind=result.worker_kind,
            worker_dead=result.worker_dead,
            job_id=result.job_id,
            request_bytes=result.request_bytes,
            otp_release=peer.otp_release,
            otp_version=peer.otp_version,
            definitions=peer.definitions,
            erts_version=peer.erts_version,
            private_home=peer.private_home,
            result=result.result,
            payload=result.payload,
            token=token,
            nodes=own.nodes,
            events=own.events,
        ).model_dump_json(),
    )
    await remote("b", Request(op="ack", incarnation=peer.identity.incarnation, token=token))


async def main() -> None:
    directory = Path(os.environ["PYBEAM_CLUSTER_DIRECTORY"])
    durable_write(directory / "a.birth.json", identity(psutil.Process()).model_dump_json())
    async with asyncio.timeout(10):
        while True:
            joined = await local(Request(op="connect", alias="b"))
            if joined.status == "ok" and (directory / "b.identity.json").exists():
                break
            await asyncio.sleep(0.05)
    peer = await remote("b", Request(op="snapshot"))
    if peer.identity is None:
        raise RuntimeError("No recipient identity after join")
    await controls(directory, peer.identity.incarnation)
    old_incarnation = peer.identity.incarnation
    await sample(directory, "first")
    first_proof = Reply.model_validate_json((directory / "first.json").read_text())
    previous: HandlerIdentity | None = None
    inflight: Request | None = None
    while True:
        state = await local(Request(op="snapshot"))
        durable_write(directory / "a.state.json", state.model_dump_json())
        if (directory / "start-inflight").exists():
            (directory / "start-inflight").unlink()
            payload = JobRequest(value=99, delay_ms=2000).model_dump_json()
            claim = await remote("b", reservation(old_incarnation, "interrupted", payload))
            if claim.status != "ok" or claim.token is None:
                raise RuntimeError("In-flight control reservation failed")
            accepted = await remote(
                "b",
                Request(
                    op="submit", incarnation=old_incarnation, token=claim.token, payload=payload
                ),
            )
            if accepted.status != "ok":
                raise RuntimeError("In-flight control admission is uncertain")
            inflight = Request(op="result", incarnation=old_incarnation, token=claim.token)
            async with asyncio.timeout(2):
                while True:
                    running = await remote("b", inflight)
                    if running.worker is not None and running.status == "pending":
                        break
                    await asyncio.sleep(0.01)
            durable_write(
                directory / "inflight.json",
                running.model_copy(update={"token": claim.token}).model_dump_json(),
            )
        if (directory / "observe-absent").exists():
            absent = await remote("b", Request(op="receipt", id="absent", ack_alias="a"))
            durable_write(directory / "absent.json", absent.model_dump_json())
            if inflight is not None:
                lost = await remote("b", inflight)
                durable_write(directory / "inflight-loss.json", lost.model_dump_json())
            (directory / "observe-absent").unlink()
        if (directory / "try-join").exists():
            (directory / "try-join").unlink()
            await local(Request(op="connect", alias="b"))
            refused = await remote("b", Request(op="receipt", id="cookie-control", ack_alias="a"))
            durable_write(directory / "join-control.json", refused.model_dump_json())
        if (directory / "rejoin").exists():
            (directory / "rejoin").unlink()
            previous = HandlerIdentity.model_validate_json(
                (directory / "b.identity.json").read_text()
            )
            await local(Request(op="connect", alias="b"))
            peer = await remote("b", Request(op="snapshot"))
            if peer.identity != previous or peer.count != 0 or peer.completed != 0:
                raise RuntimeError("Restart did not expose fresh recipient state")
            stale = await remote(
                "b", Request(op="ack", incarnation=old_incarnation, token=first_proof.token)
            )
            if stale.status != "stale":
                raise RuntimeError("Delayed old-generation ACK was accepted")
            durable_write(directory / "stale.json", stale.model_dump_json())
            durable_write(directory / "fresh.json", peer.model_dump_json())
            await sample(directory, "second")
        await asyncio.sleep(0.05)


run(main)
