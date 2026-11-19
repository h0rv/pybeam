from typing import Literal

from pydantic import BaseModel, Field, computed_field

type Scenario = Literal["normal", "hang", "burn", "crash"]
type Status = Literal["waiting", "running", "stopped", "failed", "completed"]
type Node = Literal["a", "b", "c"]
type ToolName = Literal["lookup", "hang", "burn", "crash"]

TERMINAL = frozenset({"stopped", "failed", "completed"})


class ToolRequest(BaseModel):
    session: str
    call_id: str
    name: ToolName
    audit_directory: str


class LedgerEntry(BaseModel):
    name: ToolName
    state: Literal["started", "done", "interrupted", "failed"] = "started"
    result: str = ""
    pid: int | None = None


class TranscriptEntry(BaseModel):
    kind: Literal[
        "text", "tool_started", "tool_done", "tool_interrupted", "error", "node", "stopped"
    ]
    text: str


class Snapshot(BaseModel):
    id: str = ""
    prompt: str = ""
    node: Node = "a"
    state: Status = "waiting"
    revision: int = 0
    history: str = "[]"
    ledger: dict[str, LedgerEntry] = Field(default_factory=dict)
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    token_times: list[float] = Field(default_factory=list)


class Announcement(BaseModel):
    id: str
    prompt: str
    owner: Node


class Execution(BaseModel):
    pid: int
    entered: float


class SessionView(BaseModel):
    snapshot: Snapshot
    executions: dict[str, list[Execution]]

    @computed_field
    @property
    def counts(self) -> dict[str, int]:
        return {call_id: len(events) for call_id, events in self.executions.items()}
