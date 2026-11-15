import asyncio
import os
import sys
from pathlib import Path
from typing import Annotated, Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from pybeam.distribution import Reply, Request, local
from pybeam.runtime.ownership import identity
from pybeam.tasks import TaskFunction, TaskLimits, TaskRef, TaskSupervisor


class JobRequest(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    value: Annotated[int, Field(ge=0, le=1000)]
    delay_ms: Annotated[int, Field(ge=50, le=2000)] = 100


class JobResult(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    value: int
    side_effect_count: int
    python_version: str


class EffectState(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    effects: int
    cleanups: int


class CancellationResult(EffectState):
    cancelled: Literal[True]


async def serve() -> None:
    snapshot = await local(Request(op="snapshot"))
    handler = snapshot.identity
    if handler is None:
        raise RuntimeError("Missing local handler identity")
    directory = Path(os.environ["PYBEAM_CLUSTER_DIRECTORY"])
    effects = 0
    cleanups = 0

    async def double(request: JobRequest) -> JobResult:
        nonlocal effects, cleanups
        try:
            await asyncio.sleep(request.delay_ms / 1000)
            effects += 1
            return JobResult(
                value=request.value * 2,
                side_effect_count=effects,
                python_version=sys.version.split()[0],
            )
        finally:
            await asyncio.sleep(0.03)
            cleanups += 1

    spec = TaskFunction(double, TypeAdapter(JobRequest), TypeAdapter(JobResult))
    active: dict[str, TaskRef[JobResult]] = {}
    limits = TaskLimits(capacity=snapshot.capacity, max_bytes=snapshot.max_bytes, shutdown_ms=1000)
    async with TaskSupervisor(limits) as supervisor:
        registered = await local(Request(op="register", definition="double"))
        if registered.status != "ok":
            raise RuntimeError("Recipient definition registration failed")
        (directory / "b.identity.json").write_text(handler.model_dump_json())
        (directory / "b.birth.json").write_text(identity(psutil.Process()).model_dump_json())
        async with asyncio.TaskGroup() as observers:

            async def execute(token: str, payload: str) -> None:
                from erlang import async_call
                from erlang.tasks import UncertainInflight

                try:
                    request = JobRequest.model_validate_json(payload)
                except ValidationError:
                    await local(Request(op="reject", incarnation=handler.incarnation, token=token))
                    return
                if len(spec.encode(request)) > len(payload.encode()):
                    await local(Request(op="reject", incarnation=handler.incarnation, token=token))
                    return
                ref = await supervisor.start(spec, request)
                started: object = await async_call(
                    "pybeam_dist_started", handler.incarnation.encode(), token.encode(), ref.pid
                )
                if (
                    not isinstance(started, str | bytes)
                    or Reply.model_validate_json(started).status != "ok"
                ):
                    raise RuntimeError("Cannot prove live local Task PID")
                active[token] = ref
                try:
                    result = (await ref).model_dump_json()
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is None or current.cancelling():
                        raise
                    result = CancellationResult(
                        cancelled=True, effects=effects, cleanups=cleanups
                    ).model_dump_json()
                except UncertainInflight:
                    raise
                except Exception:
                    result = '{"error":"recipient_failure"}'
                finally:
                    active.pop(token, None)
                completed = await local(
                    Request(
                        op="complete", incarnation=handler.incarnation, token=token, result=result
                    )
                )
                if completed.status != "ok":
                    raise RuntimeError("Recipient completion was not accepted")

            try:
                while True:
                    for token, ref in tuple(active.items()):
                        state = await local(
                            Request(op="cancelled", incarnation=handler.incarnation, token=token)
                        )
                        if state.cancel:
                            await ref.cancel()
                    incoming = await local(Request(op="next"))
                    if (
                        incoming.status == "ok"
                        and incoming.token is not None
                        and incoming.payload is not None
                    ):
                        observers.create_task(execute(incoming.token, incoming.payload))
                    await asyncio.sleep(0.02)
            finally:
                await local(Request(op="close_admission"))
