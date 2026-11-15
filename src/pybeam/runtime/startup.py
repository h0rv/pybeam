from typing import Annotated, Literal, Self, assert_never

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pybeam.runtime.ownership import Identity, Registry


class StartupRecord(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class ServiceEntry(StartupRecord):
    kind: Literal["service"] = "service"


class LauncherEntry(StartupRecord):
    kind: Literal["launcher"] = "launcher"
    alias: str


class VmEntry(StartupRecord):
    kind: Literal["vm"] = "vm"
    alias: str
    launcher: Identity


type SpawnEntry = Annotated[ServiceEntry | LauncherEntry, Field(discriminator="kind")]
type RegisteredEntry = Annotated[ServiceEntry | VmEntry, Field(discriminator="kind")]


class StartupPlan(StartupRecord):
    aliases: Annotated[tuple[str, ...], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def distinct(self) -> Self:
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("Startup plan has duplicate aliases")
        return self


class PendingLauncher(StartupRecord):
    kind: Literal["pending"] = "pending"
    alias: str
    launcher: Identity


class RegisteredLauncher(StartupRecord):
    kind: Literal["registered"] = "registered"
    alias: str
    launcher: Identity
    vm: Identity


type LauncherBirth = Annotated[PendingLauncher | RegisteredLauncher, Field(discriminator="kind")]


class NoVmStarted(StartupRecord):
    kind: Literal["not_started"] = "not_started"


class PartialStartup(StartupRecord):
    kind: Literal["partial"] = "partial"
    launches: Annotated[tuple[LauncherBirth, ...], Field(min_length=1)]


class CompleteStartup(StartupRecord):
    kind: Literal["complete"] = "complete"
    launches: Annotated[tuple[RegisteredLauncher, ...], Field(min_length=1)]


type StartupProgress = Annotated[
    NoVmStarted | PartialStartup | CompleteStartup, Field(discriminator="kind")
]


def progress(plan: StartupPlan, launches: tuple[LauncherBirth, ...]) -> StartupProgress:
    if not launches:
        return NoVmStarted()
    registered = tuple(item for item in launches if isinstance(item, RegisteredLauncher))
    if len(registered) == len(launches) and {item.alias for item in registered} == set(
        plan.aliases
    ):
        return CompleteStartup(launches=registered)
    return PartialStartup(launches=launches)


class ManagedStartupCensus(StartupRecord):
    owner: Identity
    guardian: Identity
    plan: StartupPlan
    startup: StartupProgress
    registry: Registry
    forced_cleanup_count: Annotated[int, Field(ge=0)]

    @property
    def launches(self) -> tuple[LauncherBirth, ...]:
        match self.startup:
            case NoVmStarted():
                return ()
            case PartialStartup(launches=launches) | CompleteStartup(launches=launches):
                return launches
            case other:
                assert_never(other)

    @model_validator(mode="after")
    def observed_births(self) -> Self:
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
        return self
