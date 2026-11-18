import asyncio
import os
from dataclasses import replace
from pathlib import Path

from examples.distributed.jobs import JobRequest
from examples.distributed.probes import controls
from pybeam import run
from pybeam.codec import Json
from pybeam.distribution import HandlerIdentity, Reply, Request, local, remote, reservation
from pybeam.runtime.ownership import Identity, durable_write, identity


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
    if receipt.status != "ok" or receipt.receipt != replace(peer.identity, **{"message_id": stage}):
        raise RuntimeError("Receipt was not produced by the recipient handler")
    ack = receipt.reverse_ack
    if (
        ack is None
        or ack.status != "ok"
        or ack.receipt is None
        or ack.receipt.node != own.identity.node
    ):
        raise RuntimeError("B did not receive the reverse OTP acknowledgment")
    payload = Json(JobRequest).encode(JobRequest(value=21, delay_ms=100)).decode()
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
        Json(
            type(
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
                )
            )
        )
        .encode(
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
            )
        )
        .decode(),
    )
    await remote("b", Request(op="ack", incarnation=peer.identity.incarnation, token=token))


async def main() -> None:
    directory = Path(os.environ["PYBEAM_CLUSTER_DIRECTORY"])
    durable_write(
        directory / "a.birth.json",
        Json(Identity).encode(identity(os.getpid())).decode(),
    )
    while True:
        if (directory / "b.identity.json").exists():
            break
        await asyncio.sleep(0.05)
    async with asyncio.timeout(10):
        while True:
            joined = await local(Request(op="connect", alias="b"))
            if joined.status == "ok":
                break
            await asyncio.sleep(0.05)
    peer = await remote("b", Request(op="snapshot"))
    if peer.identity is None:
        raise RuntimeError("No recipient identity after join")
    await controls(directory, peer.identity.incarnation)
    old_incarnation = peer.identity.incarnation
    await sample(directory, "first")
    first_proof = Json(Reply).decode((directory / "first.json").read_text())
    previous: HandlerIdentity | None = None
    inflight: Request | None = None
    while True:
        state = await local(Request(op="snapshot"))
        durable_write(directory / "a.state.json", Json(type(state)).encode(state).decode())
        if (directory / "start-inflight").exists():
            (directory / "start-inflight").unlink()
            payload = Json(JobRequest).encode(JobRequest(value=99, delay_ms=2000)).decode()
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
                Json(type(replace(running, token=claim.token)))
                .encode(replace(running, token=claim.token))
                .decode(),
            )
        if (directory / "observe-absent").exists():
            absent = await remote("b", Request(op="receipt", id="absent", ack_alias="a"))
            durable_write(directory / "absent.json", Json(type(absent)).encode(absent).decode())
            if inflight is not None:
                lost = await remote("b", inflight)
                durable_write(
                    directory / "inflight-loss.json", Json(type(lost)).encode(lost).decode()
                )
            (directory / "observe-absent").unlink()
        if (directory / "try-join").exists():
            (directory / "try-join").unlink()
            await local(Request(op="connect", alias="b"))
            refused = await remote("b", Request(op="receipt", id="cookie-control", ack_alias="a"))
            durable_write(
                directory / "join-control.json", Json(type(refused)).encode(refused).decode()
            )
        if (directory / "rejoin").exists():
            (directory / "rejoin").unlink()
            previous = Json(HandlerIdentity).decode((directory / "b.identity.json").read_text())
            await local(Request(op="connect", alias="b"))
            peer = await remote("b", Request(op="snapshot"))
            if peer.identity != previous or peer.count != 0 or peer.completed != 0:
                raise RuntimeError("Restart did not expose fresh recipient state")
            stale = await remote(
                "b", Request(op="ack", incarnation=old_incarnation, token=first_proof.token)
            )
            if stale.status != "stale":
                raise RuntimeError("Delayed old-generation ACK was accepted")
            durable_write(directory / "stale.json", Json(type(stale)).encode(stale).decode())
            durable_write(directory / "fresh.json", Json(type(peer)).encode(peer).decode())
            await sample(directory, "second")
        await asyncio.sleep(0.05)


run(main)
