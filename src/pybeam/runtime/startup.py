from dataclasses import dataclass
from typing import Literal, assert_never

from pybeam.codec import Json
from pybeam.runtime.ownership import Identity, Registry


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceEntry:
    kind: Literal["service"] = "service"


@dataclass(frozen=True, slots=True, kw_only=True)
class LauncherEntry:
    kind: Literal["launcher"] = "launcher"
    alias: str


@dataclass(frozen=True, slots=True, kw_only=True)
class VmEntry:
    kind: Literal["vm"] = "vm"
    alias: str
    launcher: Identity


type SpawnEntry = ServiceEntry | LauncherEntry
type RegisteredEntry = ServiceEntry | VmEntry


@dataclass(frozen=True, slots=True, kw_only=True)
class StartupPlan:
    aliases: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 1 <= len(self.aliases) <= 2:
            raise ValueError("Startup plan needs one or two aliases")
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("Startup plan has duplicate aliases")


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingLauncher:
    kind: Literal["pending"] = "pending"
    alias: str
    launcher: Identity


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisteredLauncher:
    kind: Literal["registered"] = "registered"
    alias: str
    launcher: Identity
    vm: Identity


type LauncherBirth = PendingLauncher | RegisteredLauncher


@dataclass(frozen=True, slots=True, kw_only=True)
class NoVmStarted:
    kind: Literal["not_started"] = "not_started"


@dataclass(frozen=True, slots=True, kw_only=True)
class PartialStartup:
    kind: Literal["partial"] = "partial"
    launches: tuple[LauncherBirth, ...]

    def __post_init__(self) -> None:
        if not self.launches:
            raise ValueError("Partial startup needs a launch")


@dataclass(frozen=True, slots=True, kw_only=True)
class CompleteStartup:
    kind: Literal["complete"] = "complete"
    launches: tuple[RegisteredLauncher, ...]

    def __post_init__(self) -> None:
        if not self.launches:
            raise ValueError("Complete startup needs a launch")


type StartupProgress = NoVmStarted | PartialStartup | CompleteStartup


def progress(plan: StartupPlan, launches: tuple[LauncherBirth, ...]) -> StartupProgress:
    if not launches:
        return NoVmStarted()
    registered = tuple(item for item in launches if isinstance(item, RegisteredLauncher))
    if len(registered) == len(launches) and {item.alias for item in registered} == set(
        plan.aliases
    ):
        return CompleteStartup(launches=registered)
    return PartialStartup(launches=launches)


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedStartupCensus:
    owner: Identity
    guardian: Identity
    plan: StartupPlan
    startup: StartupProgress
    registry: Registry
    forced_cleanup_count: int

    @property
    def launches(self) -> tuple[LauncherBirth, ...]:
        match self.startup:
            case NoVmStarted():
                return ()
            case PartialStartup(launches=launches) | CompleteStartup(launches=launches):
                return launches
            case other:
                assert_never(other)

    def __post_init__(self) -> None:
        if self.forced_cleanup_count < 0:
            raise ValueError("forced_cleanup_count must not be negative")
        launches = self.launches
        if progress(self.plan, launches) != self.startup:
            raise ValueError("Startup state disagrees with independently observed launches")
        if not {item.alias for item in launches}.issubset(self.plan.aliases):
            raise ValueError("Launcher alias is absent from the startup plan")
        births = tuple(item.launcher for item in launches)
        vms = tuple(item.vm for item in launches if isinstance(item, RegisteredLauncher))
        if len({*births, *vms}) != len(births) + len(vms):
            raise ValueError("Startup census repeats a launcher or VM birth")
        if not {*births, *vms}.issubset(self.registry.owned):
            raise ValueError("Startup census lacks registered launcher or VM ownership")
        if self.owner == self.guardian or {self.owner, self.guardian}.intersection(
            self.registry.owned
        ):
            raise ValueError("Startup census mixes owner, guardian and child births")


STARTUP_PLAN = Json(StartupPlan)
CENSUS = Json(ManagedStartupCensus)
