import asyncio
import os
import socket
import time

from celery import Celery
from celery.signals import task_revoked
from celery.worker.request import Request
from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta
from redis.asyncio import Redis

from examples.agents.agent import agent
from examples.agents.protocol import (
    TERMINAL,
    Execution,
    LedgerEntry,
    ToolName,
    ToolRequest,
    TranscriptEntry,
)
from examples.agents.tools import execute
from examples.agents.usual import store
from examples.agents.usual.models import Turn

TURN = "agents.turn"
queue = Celery("agents", broker=store.REDIS_URL, backend=store.REDIS_URL)
queue.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 60},
    result_backend_transport_options={"visibility_timeout": 60},
    visibility_timeout=60,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
)


class Runner:
    def __init__(self, kv: Redis, turn: Turn) -> None:
        self.kv, self.turn = kv, turn

    async def token(self, text: str) -> None:
        if self.turn.transcript and self.turn.transcript[-1].kind == "text":
            self.turn.transcript[-1].text += text
        else:
            self.turn.transcript.append(TranscriptEntry(kind="text", text=text))
        self.turn.token_times.append(time.monotonic())
        await store.save(self.kv, self.turn)

    async def execute(self, name: ToolName, call_id: str) -> str:
        self.turn.ledger[call_id] = LedgerEntry(name=name, pid=os.getpid())
        self.turn.transcript.append(
            TranscriptEntry(kind="tool_started", text=call_id + ": " + name + " started")
        )
        await store.save(self.kv, self.turn)
        event = Execution(pid=os.getpid(), entered=time.monotonic())
        async with self.kv.pipeline(transaction=True) as pipe:
            pipe.hincrby("agents:counts:" + self.turn.id, call_id, 1)
            pipe.rpush("agents:executions:" + self.turn.id, call_id + " " + event.model_dump_json())
            await pipe.execute()
        result = execute(
            ToolRequest(
                session=self.turn.id, call_id=call_id, name=name, audit_directory="/data/audit"
            )
        )
        self.turn.ledger[call_id] = LedgerEntry(
            name=name, state="done", result=result, pid=os.getpid()
        )
        self.turn.transcript.append(TranscriptEntry(kind="tool_done", text=call_id + ": " + result))
        await store.save(self.kv, self.turn)
        return result


async def run_turn(session: str) -> None:
    async with Redis.from_url(store.REDIS_URL) as kv:
        previous = await store.read(kv, session)
        if previous is None:
            raise ValueError("Unknown session: " + session)
        if await kv.exists("agents:stop:" + session):
            return
        turn = Turn(
            id=session,
            prompt=previous.prompt,
            node=socket.gethostname(),
            state="running",
            pid=os.getpid(),
            attempts=await kv.incr("agents:attempts:" + session),
            transcript=previous.transcript,
            token_times=previous.token_times,
        )
        turn.transcript.append(
            TranscriptEntry(
                kind="node",
                text="Attempt "
                + str(turn.attempts)
                + " started on "
                + turn.node
                + "; turn starts from the beginning",
            )
        )
        await store.save(kv, turn)
        runner = Runner(kv, turn)
        try:
            async with agent.iter(turn.prompt, deps=runner) as run:
                async for node in run:
                    turn.history = run.all_messages_json().decode()
                    await store.save(kv, turn)
                    if agent.is_model_request_node(node):
                        async with node.stream(run.ctx) as events:
                            async for event in events:
                                if isinstance(event, PartStartEvent) and isinstance(
                                    event.part, TextPart
                                ):
                                    await runner.token(event.part.content)
                                elif isinstance(event, PartDeltaEvent) and isinstance(
                                    event.delta, TextPartDelta
                                ):
                                    await runner.token(event.delta.content_delta)
                turn.history = run.all_messages_json().decode()
            turn.state = "completed"
        except Exception as error:
            turn.state = "failed"
            turn.transcript.append(TranscriptEntry(kind="error", text=str(error)))
            await store.save(kv, turn)
            raise
        await store.save(kv, turn)


def turn_task(session: str) -> None:
    asyncio.run(run_turn(session))


queue.task(name=TURN)(turn_task)


async def stopped(session: str, *, terminated: bool) -> None:
    async with Redis.from_url(store.REDIS_URL) as kv:
        turn = await store.read(kv, session)
        if turn is not None and turn.state not in TERMINAL:
            turn.stop_requested, turn.state = True, "stopped"
            text = (
                "Worker acknowledged task revocation with termination."
                if terminated
                else "Worker discarded the revoked task before execution."
            )
            turn.transcript.append(TranscriptEntry(kind="stopped", text=text))
            await store.save(kv, turn)


def revoked(request: Request, *, terminated: bool, **_kwargs: object) -> None:
    asyncio.run(stopped(request.id, terminated=terminated))


task_revoked.connect(revoked, weak=False)
