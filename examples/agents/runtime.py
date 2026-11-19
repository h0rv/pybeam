import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from examples.agents.agent import agent
from examples.agents.protocol import (
    TERMINAL,
    Announcement,
    LedgerEntry,
    Node,
    Snapshot,
    ToolName,
    ToolRequest,
    TranscriptEntry,
)
from examples.agents.session import Session
from examples.agents.tools import isolated
from pybeam import ActorGroup
from pybeam.codec import Json
from pybeam.integrations.hornbeam import Services
from pybeam.tasks import ExecutionMode, TaskCrashed, TaskFunction, TaskLimits, TaskSupervisor

ANNOUNCEMENTS = "agents:new"
SNAPSHOTS = "agents:snapshots"
SNAPSHOT_PENDING_BYTES = 1024 * 1024
TOOL = TaskFunction(isolated, Json(str), Json(str), execution=ExecutionMode.ISOLATED)
logger = logging.getLogger(__name__)


def topic(session: str) -> str:
    return "agents:transcript:" + session


def stop_topic(session: str) -> str:
    return "agents:stop:" + session


@dataclass(slots=True)
class Placement:
    task: asyncio.Task[None]
    owned: bool = False


class Runner:
    def __init__(self, runtime: Runtime, actor: Session, snapshot: Snapshot) -> None:
        self.runtime = runtime
        self.actor = actor
        self.snapshot = snapshot

    def append(self, kind: str, text: str) -> None:
        self.snapshot.transcript.append(
            TranscriptEntry.model_validate({"kind": kind, "text": text})
        )

    async def save(self) -> None:
        self.snapshot = Snapshot.model_validate_json(
            await self.actor.save(self.snapshot.model_dump_json())
        )
        await self.runtime.broadcast(self.snapshot)

    async def token(self, text: str) -> None:
        if self.snapshot.transcript and self.snapshot.transcript[-1].kind == "text":
            self.snapshot.transcript[-1].text += text
        else:
            self.append("text", text)
        self.snapshot.token_times.append(time.monotonic())
        await self.save()

    async def execute(self, name: ToolName, call_id: str) -> str:
        previous = self.snapshot.ledger.get(call_id)
        if previous is not None:
            if previous.state == "started":
                previous.state = "interrupted"
                previous.result = "Interrupted by node loss; not re-run"
                self.append("tool_interrupted", call_id + ": " + previous.result)
                await self.save()
            return previous.result
        entry = LedgerEntry(name=name)
        self.snapshot.ledger[call_id] = entry
        self.append("tool_started", call_id + ": " + name + " started")
        await self.save()
        request = ToolRequest(
            session=self.snapshot.id,
            call_id=call_id,
            name=name,
            audit_directory=str(self.runtime.audit),
        )
        async with TaskSupervisor(TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=3000)) as sup:
            ref = await sup.start(TOOL, request.model_dump_json())
            try:
                inspection = await ref.inspect()
                self.snapshot.ledger[call_id].pid = inspection.os_pid
                await self.save()
                result = await ref
            except TaskCrashed as error:
                entry = self.snapshot.ledger[call_id]
                entry.state, entry.result = "failed", "Tool process crashed: " + str(error.reason)
                self.append("error", call_id + ": " + entry.result)
                await self.save()
                raise
            finally:
                await ref.terminate()
                await ref.exception()
        entry = self.snapshot.ledger[call_id]
        entry.state, entry.result = "done", result
        self.append("tool_done", call_id + ": " + result)
        await self.save()
        return result

    async def drive(self) -> None:
        try:
            messages = ModelMessagesTypeAdapter.validate_json(self.snapshot.history)
            if messages and isinstance(messages[-1], ModelResponse):
                messages.pop()
            async with agent.iter(
                self.snapshot.prompt if not messages else None,
                message_history=messages,
                deps=self,
            ) as run:
                async for node in run:
                    self.snapshot.history = run.all_messages_json().decode()
                    await self.save()
                    if agent.is_model_request_node(node):
                        async with node.stream(run.ctx) as events:
                            async for event in events:
                                if isinstance(event, PartStartEvent) and isinstance(
                                    event.part, TextPart
                                ):
                                    await self.token(event.part.content)
                                elif isinstance(event, PartDeltaEvent) and isinstance(
                                    event.delta, TextPartDelta
                                ):
                                    await self.token(event.delta.content_delta)
                self.snapshot.history = run.all_messages_json().decode()
            self.snapshot.state = "completed"
        except asyncio.CancelledError:
            for entry in self.snapshot.ledger.values():
                if entry.state == "started":
                    entry.state, entry.result = "interrupted", "Stopped; not re-run"
            self.snapshot.state = "stopped"
            self.append("stopped", "Stopped. The active tool process has been terminated.")
        except Exception as error:
            logger.exception("Session %s failed", self.snapshot.id)
            self.snapshot.state = "failed"
            self.append("error", str(error))
        await self.save()


class Runtime:
    def __init__(
        self,
        beam: Services,
        node: Node,
        replicas: list[str],
        audit: Path,
        tasks: asyncio.TaskGroup,
        render: Callable[[Snapshot], str],
    ) -> None:
        self.beam, self.node, self.replicas = beam, node, replicas
        self.audit, self.tasks, self.render = audit, tasks, render
        self.snapshots: dict[str, Snapshot] = {}
        self.placements: dict[str, Placement] = {}
        self.changed = asyncio.Condition()

    async def broadcast(self, snapshot: Snapshot) -> None:
        await self.beam.publish(SNAPSHOTS, snapshot.model_dump_json().encode())
        await self.beam.publish(topic(snapshot.id), self.render(snapshot).encode())

    async def remember(self, snapshot: Snapshot) -> None:
        previous = self.snapshots.get(snapshot.id)
        if previous is None or previous.revision <= snapshot.revision:
            async with self.changed:
                self.snapshots[snapshot.id] = snapshot
                self.changed.notify_all()
            placement = self.placements.get(snapshot.id)
            if snapshot.state in TERMINAL and placement is not None and not placement.owned:
                placement.task.cancel()

    async def announcements(self, ready: asyncio.Event) -> None:
        async with self.beam.subscribe(ANNOUNCEMENTS) as subscription:
            ready.set()
            async for payload in subscription:
                announcement = Announcement.model_validate_json(payload)
                if announcement.id in self.placements:
                    continue
                await self.remember(
                    Snapshot(
                        id=announcement.id, prompt=announcement.prompt, node=announcement.owner
                    )
                )
                task = self.tasks.create_task(self.place(announcement))
                self.placements[announcement.id] = Placement(task)

    async def updates(self, ready: asyncio.Event) -> None:
        async with self.beam.subscribe(SNAPSHOTS, max_bytes=SNAPSHOT_PENDING_BYTES) as subscription:
            ready.set()
            async for payload in subscription:
                await self.remember(Snapshot.model_validate_json(payload))

    async def place(self, announcement: Announcement) -> None:
        try:
            if self.node != announcement.owner:
                await asyncio.sleep(0.3)
            async with ActorGroup(replicas=self.replicas) as group:
                actor = await group.restore(announcement.id, Session)
                self.placements[announcement.id].owned = True
                snapshot = Snapshot.model_validate_json(await actor.read())
                if snapshot.state in TERMINAL:
                    await self.broadcast(snapshot)
                    return
                resumed = bool(snapshot.id)
                snapshot.id, snapshot.prompt = announcement.id, announcement.prompt
                snapshot.node, snapshot.state = self.node, "running"
                runner = Runner(self, actor, snapshot)
                runner.append(
                    "node", ("Resumed" if resumed else "Started") + " on node " + self.node
                )
                await runner.save()
                async with (
                    self.beam.subscribe(stop_topic(snapshot.id)) as stop,
                    asyncio.TaskGroup() as tasks,
                ):
                    running = tasks.create_task(runner.drive())

                    async def listen() -> None:
                        await stop.receive()
                        running.cancel()

                    listener = tasks.create_task(listen())
                    await running
                    listener.cancel()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Session placement failed: %s", announcement.id)

    async def wait(self, session: str, *, terminal: bool = False) -> Snapshot:
        async with asyncio.timeout(10), self.changed:
            await self.changed.wait_for(
                lambda: (
                    session in self.snapshots
                    and (not terminal or self.snapshots[session].state in TERMINAL)
                )
            )
            return self.snapshots[session]
