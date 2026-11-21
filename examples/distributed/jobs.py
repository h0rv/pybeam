import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pybeam.codec import Json
from pybeam.distribution import Reply, Request, local
from pybeam.runtime.ownership import Identity, durable_write, identity
from pybeam.tasks import TaskFunction, TaskLimits, TaskRef, TaskSupervisor


@dataclass(frozen=True, slots=True, kw_only=True)
class JobRequest:
    value: int
    delay_ms: int = 100

    def __post_init__(self) -> None:
        if type(self.value) is not int or not 0 <= self.value <= 1000:
            raise ValueError("value must be an integer from 0 to 1000")
        if type(self.delay_ms) is not int or not 50 <= self.delay_ms <= 2000:
            raise ValueError("delay_ms must be an integer from 50 to 2000")


@dataclass(frozen=True, slots=True, kw_only=True)
class JobResult:
    value: int
    side_effect_count: int
    python_version: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EffectState:
    effects: int
    cleanups: int


@dataclass(frozen=True, slots=True, kw_only=True)
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

    spec = TaskFunction(double, Json(JobRequest), Json(JobResult))
    active: dict[str, TaskRef[JobResult]] = {}
    limits = TaskLimits(capacity=1, max_bytes=snapshot.max_bytes, shutdown_ms=1000)
    async with asyncio.TaskGroup():
        registered = await local(Request(op="register", definition="double"))
        if registered.status != "ok":
            raise RuntimeError("Recipient definition registration failed")
        durable_write(directory / "b.identity.json", Json(type(handler)).encode(handler).decode())
        durable_write(
            directory / "b.birth.json", Json(Identity).encode(identity(os.getpid())).decode()
        )
        async with asyncio.TaskGroup() as observers:

            async def execute(token: str, payload: str) -> None:
                from erlang import async_call
                from erlang.tasks import UncertainInflight

                try:
                    request = Json(JobRequest).decode(payload)
                except ValueError:
                    await local(Request(op="reject", incarnation=handler.incarnation, token=token))
                    return
                encoded = spec.encode(request)
                if len(encoded) > len(payload.encode()):
                    await local(Request(op="reject", incarnation=handler.incarnation, token=token))
                    return
                async with TaskSupervisor(limits) as supervisor:
                    bound: object = await async_call(
                        "pybeam_dist_bind",
                        handler.incarnation.encode(),
                        token.encode(),
                        supervisor._group,
                    )
                    if (
                        not isinstance(bound, str | bytes)
                        or Json(Reply).decode(bound).status != "ok"
                    ):
                        raise RuntimeError("Cannot bind request to an empty local Task scope")
                    ref = await supervisor.start(spec, request)
                    started: object = await async_call(
                        "pybeam_dist_started", handler.incarnation.encode(), token.encode(), ref.pid
                    )
                    if (
                        not isinstance(started, str | bytes)
                        or Json(Reply).decode(started).status != "ok"
                    ):
                        raise RuntimeError("Cannot prove request-owned local Task PID")
                    active[token] = ref
                    try:
                        value = await ref
                        result = Json(type(value)).encode(value).decode()
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is None or current.cancelling():
                            raise
                        result = (
                            Json(
                                type(
                                    CancellationResult(
                                        cancelled=True, effects=effects, cleanups=cleanups
                                    )
                                )
                            )
                            .encode(
                                CancellationResult(
                                    cancelled=True, effects=effects, cleanups=cleanups
                                )
                            )
                            .decode()
                        )
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
