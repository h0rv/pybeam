from pydantic import BaseModel, Field, computed_field

from examples.agents.protocol import Execution, LedgerEntry, Status, TranscriptEntry


class Turn(BaseModel):
    id: str
    prompt: str
    node: str = "waiting"
    state: Status = "waiting"
    history: str = "[]"
    ledger: dict[str, LedgerEntry] = Field(default_factory=dict)
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    token_times: list[float] = Field(default_factory=list)
    attempts: int = 0
    stop_requested: bool = False
    pid: int | None = None


class View(BaseModel):
    snapshot: Turn
    executions: dict[str, list[Execution]]

    @computed_field
    @property
    def counts(self) -> dict[str, int]:
        return {call_id: len(events) for call_id, events in self.executions.items()}
