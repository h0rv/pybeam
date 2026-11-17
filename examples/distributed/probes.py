import asyncio
from pathlib import Path

from examples.distributed.jobs import CancellationResult, EffectState, JobRequest
from pybeam.codec import Json
from pybeam.distribution import Reply, Request, local, remote, reservation
from pybeam.runtime.ownership import durable_write


async def controls(directory: Path, incarnation: str) -> None:
    unknown = await remote("b", Request(op="not_an_operation"))
    if unknown.status != "unknown_operation":
        raise RuntimeError("Unknown operation accepted")
    # Unsent reservations expire, but accepted body credits cannot expire.
    payload = (
        Json(type(JobRequest(value=7, delay_ms=1000)))
        .encode(JobRequest(value=7, delay_ms=1000))
        .decode()
    )
    first = await remote("b", reservation(incarnation, "unsent-one", payload))
    second = await remote("b", reservation(incarnation, "unsent-two", payload))
    full = await remote("b", reservation(incarnation, "unsent-full", payload))
    if first.status != "ok" or second.status != "ok" or full.status != "overloaded":
        raise RuntimeError("Reservation count bound failed")
    conflict = await remote("b", reservation(incarnation, "unsent-one", payload + " "))
    if conflict.status != "conflict":
        raise RuntimeError("Conflicting duplicate ID accepted")
    await asyncio.sleep(1.1)
    expired = await remote(
        "b", Request(op="submit", incarnation=incarnation, token=first.token, payload=payload)
    )
    if expired.status != "unknown_token":
        raise RuntimeError("Expired unsent reservation accepted")
    tokens: list[str] = []
    for number in range(2):
        claim = await remote("b", reservation(incarnation, "running-" + str(number), payload))
        if claim.status != "ok" or claim.token is None:
            raise RuntimeError("Could not admit bounded control task")
        tokens.append(claim.token)
        accepted = await remote(
            "b", Request(op="submit", incarnation=incarnation, token=claim.token, payload=payload)
        )
        if accepted.status != "ok":
            raise RuntimeError("Control payload was not accepted")
    try:
        async with asyncio.timeout(0.05):
            while True:
                await remote("b", Request(op="result", incarnation=incarnation, token=tokens[0]))
                await asyncio.sleep(0.01)
    except TimeoutError:
        pass
    snapshot = await remote("b", Request(op="snapshot"))
    full = await remote("b", reservation(incarnation, "running-full", payload))
    if snapshot.count != 2 or full.status != "overloaded":
        raise RuntimeError("Timed-out observation released actual body credit")
    for token in tokens:
        await remote("b", Request(op="cancel", incarnation=incarnation, token=token))
    cleanups: list[CancellationResult] = []
    async with asyncio.timeout(4):
        for token in tokens:
            while True:
                result = await remote(
                    "b", Request(op="result", incarnation=incarnation, token=token)
                )
                if result.status == "ok":
                    if result.result is None or result.worker is None or not result.worker_dead:
                        raise RuntimeError("Body cancellation was not acknowledged")
                    cleanups.append(Json(CancellationResult).decode(result.result))
                    break
                if result.status != "pending":
                    raise RuntimeError("Control cancellation became uncertain")
                await asyncio.sleep(0.02)
    cleanup = EffectState(
        effects=max(item.effects for item in cleanups),
        cleanups=max(item.cleanups for item in cleanups),
    )
    if cleanup != EffectState(effects=0, cleanups=2):
        raise RuntimeError("Cancelled bodies did not finish their actual finally blocks")
    held = await remote("b", Request(op="snapshot"))
    if held.count != 2:
        raise RuntimeError("Completed unobserved results were not bounded/charged")
    for token in tokens:
        await remote("b", Request(op="ack", incarnation=incarnation, token=token))
    await local(Request(op="disconnect", alias="b"))
    disconnected = await remote("b", Request(op="receipt", id="disconnected", ack_alias="a"))
    if disconnected.status != "uncertain" or disconnected.receipt is not None:
        raise RuntimeError("Disconnected link silently reconnected or reported success")
    await local(Request(op="connect", alias="b"))
    durable_write(
        directory / "controls.json",
        Json(
            type(
                Reply(
                    status="ok",
                    count=held.count,
                    receipt=disconnected.receipt,
                    result=Json(type(cleanup)).encode(cleanup).decode(),
                )
            )
        )
        .encode(
            Reply(
                status="ok",
                count=held.count,
                receipt=disconnected.receipt,
                result=Json(type(cleanup)).encode(cleanup).decode(),
            )
        )
        .decode(),
    )
