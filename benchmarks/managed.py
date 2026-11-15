from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, assert_never

from pybeam.runtime.cluster import ManagedCleanupAck
from pybeam.runtime.ownership import Identity, resolve


class StopSignal(StrEnum):
    NONE = "none"
    REQUESTED = "requested"


@dataclass(frozen=True, slots=True)
class ImmediateCleanup:
    reason: str


@dataclass(frozen=True, slots=True)
class FailedManagedShutdown:
    reason: str


@dataclass(frozen=True, slots=True)
class ManagedStopped:
    ack: ManagedCleanupAck


@dataclass(frozen=True, slots=True)
class Finished:
    status: Literal["passed", "walkthrough", "unsupported"]
    reason: str | None = None


type CleanupResult = ImmediateCleanup | FailedManagedShutdown | ManagedStopped | Finished


@dataclass(frozen=True, slots=True)
class AdapterRunning:
    pass


@dataclass(frozen=True, slots=True)
class UnacknowledgedExit:
    reason: str


@dataclass(frozen=True, slots=True)
class AcknowledgedExit:
    ack: ManagedCleanupAck


type AdapterState = AdapterRunning | UnacknowledgedExit | AcknowledgedExit


@dataclass(frozen=True, slots=True)
class Active:
    def abort(self, reason: str) -> ImmediateCleanup:
        return ImmediateCleanup(reason)


@dataclass(frozen=True, slots=True)
class Draining:
    deadline: float

    def abort(self, reason: str) -> FailedManagedShutdown:
        return FailedManagedShutdown(reason)

    def advance(
        self, *, now: float, adapter: AdapterState
    ) -> Draining | ManagedStopped | FailedManagedShutdown:
        if now >= self.deadline:
            return self.abort("Managed stop deadline expired")
        match adapter:
            case AdapterRunning():
                return self
            case AcknowledgedExit(ack):
                return ManagedStopped(ack)
            case UnacknowledgedExit(reason):
                return self.abort(reason)
            case _:
                assert_never(adapter)


type ExecutionPhase = Active | Draining


def read_exit(
    directory: Path, adapter: Identity, owned: set[Identity], returncode: int
) -> AcknowledgedExit | UnacknowledgedExit:
    if returncode != 0:
        return UnacknowledgedExit("Managed adapter exited unsuccessfully")
    try:
        ack = ManagedCleanupAck.model_validate_json(
            (directory / "managed-cleanup.json").read_bytes()
        )
    except OSError, ValueError:
        return UnacknowledgedExit("Managed cleanup ACK missing or invalid")
    if (
        ack.owner != adapter
        or ack.guardian not in owned
        or not set(ack.cleanup.owned).issubset(owned)
    ):
        return UnacknowledgedExit("Managed cleanup ACK belongs to different process births")
    if any(resolve(value) is not None for value in owned):
        return UnacknowledgedExit("Managed cleanup ACK left registered processes alive")
    return AcknowledgedExit(ack)
