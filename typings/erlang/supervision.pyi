from dataclasses import dataclass, field
from enum import StrEnum

class ChildState(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    RESTARTING = "restarting"

class CapabilityUnavailable(RuntimeError): ...

class SupervisorError(RuntimeError):
    reason: object
    def __init__(self, reason: object) -> None: ...

@dataclass(frozen=True, slots=True)
class ChildSnapshot:
    child_id: bytes
    process: object | None
    state: ChildState

@dataclass(frozen=True, slots=True)
class NativeSupervisor:
    process: object
    capacity: int | None = field(kw_only=True)
    max_bytes: int | None = field(kw_only=True)
    max_children: int | None = field(kw_only=True)
    async def start_supervisor(
        self,
        child_id: bytes,
        strategy: str,
        intensity: int,
        period: int,
        *,
        capacity: int | None,
        max_bytes: int | None,
        max_children: int | None,
    ) -> NativeSupervisor: ...
    async def start_child(
        self,
        child_id: bytes,
        module: str,
        function: str,
        args: tuple[object, ...],
        restart: str,
        shutdown_ms: int,
        *,
        capacity: int | None,
        max_bytes: int | None,
    ) -> ChildSnapshot: ...
    async def children(self) -> tuple[ChildSnapshot, ...]: ...
    async def stop_child(self, child_id: bytes) -> None: ...
    async def restart_child(self, child_id: bytes) -> ChildSnapshot: ...
    async def delete_child(self, child_id: bytes) -> None: ...
