from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, assert_never

from pybeam.runtime.cluster import ManagedCleanupAck
from pybeam.runtime.ownership import Identity, resolve
from pybeam.runtime.startup import ManagedStartupCensus


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


def parse_cleanup(
    directory: Path, owned: set[Identity], returncode: int | None
) -> AcknowledgedExit | UnacknowledgedExit:
    if returncode != 0:
        return UnacknowledgedExit("Managed adapter exited unsuccessfully")
    try:
        ack = ManagedCleanupAck.model_validate_json(
            (directory / "managed-cleanup.json").read_bytes()
        )
        census = ManagedStartupCensus.model_validate_json(
            (directory / "managed-startup-census.json").read_bytes()
        )
    except OSError, ValueError:
        return UnacknowledgedExit("Managed cleanup ACK missing or invalid")
    if (
        ack.owner != census.owner
        or ack.guardian != census.guardian
        or census.owner not in owned
        or census.guardian not in owned
        or set(census.registry.owned) != owned - {census.owner, census.guardian}
    ):
        return UnacknowledgedExit("Managed cleanup ACK belongs to different process births")
    if (
        census.registry.phase != "confirmed"
        or census.forced_cleanup_count != 0
        or set(ack.cleanup.owned) != set(census.registry.owned)
        or len(ack.cleanup.owned) != len(census.registry.owned)
        or set(ack.launchers) != {item.launcher for item in census.launches}
        or {(item.alias, item.launcher) for item in ack.shutdowns}
        != {(item.alias, item.launcher) for item in census.launches}
    ):
        return UnacknowledgedExit("Managed cleanup ACK omits or changes independent startup census")
    if any(resolve(value) is not None for value in owned):
        return UnacknowledgedExit("Managed cleanup ACK left registered processes alive")
    return AcknowledgedExit(ack)


def read_exit(
    directory: Path, adapter: Identity, owned: set[Identity], returncode: int
) -> AcknowledgedExit | UnacknowledgedExit:
    result = parse_cleanup(directory, owned, returncode)
    match result:
        case AcknowledgedExit(ack):
            if ack.owner != adapter:
                return UnacknowledgedExit(
                    "Managed cleanup ACK belongs to a different adapter birth"
                )
            return result
        case UnacknowledgedExit():
            return result
        case _:
            assert_never(result)
