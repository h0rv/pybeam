import asyncio
import os
from pathlib import Path

import psutil
from pydantic import BaseModel, ConfigDict, TypeAdapter

from examples.distributed.jobs import JobRequest, JobResult, serve
from pybeam import run
from pybeam.distribution import Reply, Request, local, remote, reservation
from pybeam.runtime.ownership import durable_write, identity
from pybeam.tasks import TaskFunction, TaskLimits, TaskSupervisor


class GateInfo(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    capacity: int
    max_bytes: int
    count: int
    bytes: int
    unsettled: int
    closed: bool


class ScopeInfo(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    gate: GateInfo
    children: list[object]
    contexts: list[object]
    jobs: int


async def callback(name: str, *args: object) -> Reply:
    from erlang import async_call

    value: object = await async_call(name, *args)
    assert isinstance(value, str | bytes)
    return Reply.model_validate_json(value)


async def empty_scope(supervisor: TaskSupervisor) -> ScopeInfo:
    from erlang import async_call

    value: object = await async_call("_task_info", supervisor._group)
    info = ScopeInfo.model_validate(value)
    assert info.children == info.contexts == []
    assert info.jobs == info.gate.count == info.gate.bytes == info.gate.unsettled == 0
    return info


async def binding_controls(directory: Path) -> None:
    state = await local(Request(op="snapshot"))
    assert state.identity is not None
    inc = state.identity.incarnation
    await local(Request(op="register", definition="double"))
    payload = JobRequest(value=21, delay_ms=1000).model_dump_json()
    reserved = await local(reservation(inc, "scope-control", payload))
    assert reserved.token is not None
    token = reserved.token
    assert (
        await local(Request(op="submit", incarnation=inc, token=token, payload=payload))
    ).status == "ok"
    assert (await local(Request(op="next"))).token == token

    async def body(value: int) -> int:
        try:
            await asyncio.sleep(0.25)
            return value * 2
        finally:
            await asyncio.sleep(0.03)

    spec = TaskFunction(body, TypeAdapter(int), TypeAdapter(int))
    limits = TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=1000)
    async with TaskSupervisor(limits) as owner, TaskSupervisor(limits) as unrelated:
        assert (
            await callback("pybeam_dist_bind", inc.encode(), token.encode(), owner._group)
        ).status == "ok"
        other = await unrelated.start(spec, 21)
        assert (
            await callback("pybeam_dist_started", inc.encode(), token.encode(), other.pid)
        ).status == "uncertain"
        assert (
            await local(Request(op="complete", incarnation=inc, token=token, result="forged"))
        ).status == "invalid_phase"
        own = await owner.start(spec, 21)
        assert (
            await callback("pybeam_dist_started", inc.encode(), token.encode(), own.pid)
        ).status == "ok"
        assert (
            await callback("pybeam_dist_started", inc.encode(), token.encode(), other.pid)
        ).status == "uncertain"
        assert (
            await local(Request(op="complete", incarnation=inc, token=token, result="early"))
        ).status == "uncertain"
        assert await own == await other == 42
        own_info = await empty_scope(owner)
        other_info = await empty_scope(unrelated)
    assert (
        await local(Request(op="complete", incarnation=inc, token=token, result="42"))
    ).status == "ok"
    result = await local(Request(op="result", incarnation=inc, token=token))
    assert result.worker_dead and result.worker_kind == "temporary_otp_task"
    assert (await local(Request(op="ack", incarnation=inc, token=token))).status == "ok"
    assert (await local(Request(op="snapshot"))).count == 0
    durable_write(directory / "scope.json", own_info.model_dump_json())
    durable_write(directory / "unrelated.json", other_info.model_dump_json())


async def sender(directory: Path) -> None:
    async with asyncio.timeout(10):
        while not (directory / "b.identity.json").exists():
            await local(Request(op="connect", alias="b"))
            await asyncio.sleep(0.02)
    await local(Request(op="connect", alias="b"))
    peer = await remote("b", Request(op="snapshot"))
    own = await local(Request(op="snapshot"))
    assert peer.identity is not None and own.identity is not None
    inc = peer.identity.incarnation
    payload = JobRequest(value=21, delay_ms=1000).model_dump_json()
    reserved = await remote("b", reservation(inc, "single", payload))
    assert reserved.status == "ok" and reserved.token is not None
    token = reserved.token
    submit = Request(op="submit", incarnation=inc, token=token, payload=payload)

    async def send(_: int) -> str:
        return (await remote("b", submit)).model_dump_json()

    spec = TaskFunction(send, TypeAdapter(int), TypeAdapter(str))
    async with asyncio.TaskGroup() as barrier:
        receipt = barrier.create_task(remote("b", Request(op="receipt", id="hold", ack_alias="b")))
        await asyncio.sleep(0.05)
        assert not receipt.done()
        async with TaskSupervisor(
            TaskLimits(capacity=3, max_bytes=100, shutdown_ms=1500)
        ) as handles:
            refs = [await handles.start(spec, index) for index in range(3)]
            statuses = [Reply.model_validate_json(await ref).status for ref in refs]
            assert sorted(statuses) == ["invalid_phase", "invalid_phase", "ok"]
            info = await empty_scope(handles)
            durable_write(directory / "sender-scope.json", info.model_dump_json())
    # All three persistent native caller Tasks and their contexts have now exited.
    # B's serving loop picks the job up only after the held receipt releases it.
    async with asyncio.timeout(5):
        while True:
            running = await remote("b", Request(op="result", incarnation=inc, token=token))
            if running.worker is not None:
                break
            await asyncio.sleep(0.01)
    assert running.status == "pending"
    assert (await remote("b", Request(op="snapshot"))).count == 1
    assert (await remote("b", reservation(inc, "quota-shared", payload))).status == "overloaded"
    assert (
        await remote("b", Request(op="ack", incarnation=inc, token=token))
    ).status == "invalid_phase"
    # Drop all result observations until after the effect/finally/context cleanup.
    await asyncio.sleep(1.25)
    retained = await remote("b", Request(op="snapshot"))
    assert retained.count == 1 and retained.completed == peer.completed + 1
    assert (await remote("b", submit)).status == "invalid_phase"
    result = await remote("b", Request(op="result", incarnation=inc, token=token))
    assert result.identity == peer.identity and result.payload == payload
    assert result.job_id == "single" and result.request_bytes == len(payload.encode())
    assert result.worker == running.worker and result.worker_dead
    assert result.result is not None
    assert JobResult.model_validate_json(result.result) == JobResult(
        value=42, side_effect_count=1, python_version="3.14.10"
    )
    receipt = await remote("b", Request(op="receipt", id="terminal", ack_alias="a"))
    assert receipt.receipt == peer.identity.model_copy(update={"message_id": "terminal"})
    assert receipt.reverse_ack is not None
    assert receipt.reverse_ack.receipt == own.identity.model_copy(update={"message_id": "terminal"})
    assert (
        await remote("b", Request(op="ack", incarnation="0" * 32, token=token))
    ).status == "stale"
    assert (await remote("b", Request(op="snapshot"))).count == 1
    assert (await remote("b", Request(op="ack", incarnation=inc, token=token))).status == "ok"
    assert (await remote("b", Request(op="snapshot"))).count == 0
    assert (await remote("b", submit)).status == "unknown_token"
    durable_write(directory / "admission.json", result.model_dump_json())


async def main() -> None:
    directory = Path(os.environ["PYBEAM_CLUSTER_DIRECTORY"])
    alias = os.environ["PYBEAM_NODE_ALIAS"]
    durable_write(directory / (alias + ".birth.json"), identity(psutil.Process()).model_dump_json())
    if alias == "b":
        await binding_controls(directory)
        await serve()
    else:
        await sender(directory)
        await asyncio.sleep(30)


if __name__ == "__main__":
    run(main)
