import contextlib
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, assert_never

from pybeam.codec import Json
from pybeam.runtime.ownership import (
    IDENTITY,
    REGISTRY,
    Identity,
    Registry,
    ancestors,
    cleanup,
    discover,
    durable_write,
    identity,
    record,
    resolve,
)
from pybeam.runtime.startup import (
    CENSUS,
    STARTUP_PLAN,
    LauncherBirth,
    LauncherEntry,
    ManagedStartupCensus,
    PendingLauncher,
    RegisteredEntry,
    RegisteredLauncher,
    ServiceEntry,
    SpawnEntry,
    StartupPlan,
    VmEntry,
    progress,
)

_SERVICE_ENTRY = ServiceEntry()
_GENERATION = re.compile(r"[0-9a-f]{32}")


class RegistrationPhase(StrEnum):
    OPEN = "open"
    SEALED = "sealed"


@dataclass(frozen=True, slots=True, kw_only=True)
class SpawnRequest:
    argv: tuple[str, ...]
    entry: SpawnEntry = _SERVICE_ENTRY

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("Spawn request needs a command")


@dataclass(frozen=True, slots=True, kw_only=True)
class SpawnReply:
    identity: Identity


class AuthorizationError(RuntimeError):
    pass


class StopRequested(AuthorizationError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class ImmediateStop:
    kind: Literal["immediate"] = "immediate"


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedStop:
    kind: Literal["managed"] = "managed"
    node_shutdown_seconds: float = 5.0
    guardian_seconds: Literal[8] = 8

    def __post_init__(self) -> None:
        if not 0 < self.node_shutdown_seconds <= 5:
            raise ValueError("node_shutdown_seconds must be greater than 0 and at most 5")

    @property
    def grace_seconds(self) -> float:
        return 2 * (self.node_shutdown_seconds + 2) + self.guardian_seconds

    @property
    def guardian_grace_seconds(self) -> float:
        return self.guardian_seconds - 2


type StopPolicy = ImmediateStop | ManagedStop
_IMMEDIATE = ImmediateStop()


@dataclass(frozen=True, slots=True, kw_only=True)
class GuardianExit:
    registry: Registry
    forced_cleanup_count: int

    def __post_init__(self) -> None:
        if self.forced_cleanup_count < 0:
            raise ValueError("forced_cleanup_count must not be negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class Registration:
    process: Identity
    owner: Identity
    guardian: Identity
    generation: str
    entry: RegisteredEntry = _SERVICE_ENTRY

    def __post_init__(self) -> None:
        if _GENERATION.fullmatch(self.generation) is None:
            raise ValueError("generation must be 32 lowercase hexadecimal digits")


@dataclass(frozen=True, slots=True, kw_only=True)
class GuardianEndpoint:
    """Borrow an existing local guardian's registration directory and lifetime.

    The borrower never starts or stops this guardian. Its owner must durably
    record and acknowledge each birth before any nested entry is authorized.
    """

    directory: Path
    owner: Identity
    guardian: Identity
    stop_file: Path | None = None
    control_owner: Identity | None = None
    stop_policy: StopPolicy = _IMMEDIATE

    def require_lifetime(self) -> None:
        _require_live(self.owner, "Outer owner exited before execution authorization")
        _require_live(self.guardian, "Outer guardian exited before execution authorization")
        if self.control_owner is not None:
            _require_live(self.control_owner, "Outer control owner exited before authorization")

    def stopping(self) -> bool:
        return (self.directory / "registration.closed").exists() or (
            self.stop_file is not None and self.stop_file.exists()
        )

    def require_active(self) -> None:
        self.require_lifetime()
        if self.stopping():
            raise StopRequested("Outer owner cancelled execution authorization")


_REGISTRATION = Json(Registration)
_SPAWN_REQUEST = Json(SpawnRequest)
_SPAWN_REPLY = Json(SpawnReply)
ENDPOINT = Json(GuardianEndpoint)
GUARDIAN_EXIT = Json(GuardianExit)


@dataclass(frozen=True, slots=True)
class _NoCensus:
    pass


@dataclass(frozen=True, slots=True)
class _CensusTarget:
    endpoint: GuardianEndpoint
    plan: StartupPlan


def observe_vm(launches: list[LauncherBirth], registration: Registration) -> None:
    match registration.entry:
        case ServiceEntry():
            return
        case VmEntry(alias=alias, launcher=launcher):
            for index, item in enumerate(launches):
                if item.launcher == launcher:
                    if item.alias != alias or isinstance(item, RegisteredLauncher):
                        raise AuthorizationError("VM registration disagrees with launcher census")
                    launches[index] = RegisteredLauncher(
                        alias=alias, launcher=launcher, vm=registration.process
                    )
                    return
            raise AuthorizationError("VM has no independently spawned launcher")
        case other:
            assert_never(other)


def _ignore_registration(registration: Registration) -> None:
    pass


def register_borrowed(endpoint: GuardianEndpoint, local: Registration) -> None:
    endpoint.require_active()
    _authorize_execution(local)
    registration = Registration(
        process=local.process,
        owner=endpoint.owner,
        guardian=endpoint.guardian,
        generation=secrets.token_hex(16),
    )
    path = endpoint.directory / ("register-" + str(local.process.pid) + ".json")
    durable_write(path, _REGISTRATION.encode(registration).decode())
    ack = path.with_suffix(".ack")
    deadline = time.monotonic() + 5
    while not ack.exists():
        endpoint.require_active()
        _authorize_execution(local)
        if time.monotonic() > deadline:
            raise AuthorizationError("No outer guardian acknowledgment; entry is sealed")
        time.sleep(0.01)
    if _REGISTRATION.decode(ack.read_text()) != registration:
        raise AuthorizationError("Outer acknowledgment has a different identity or generation")
    ack.unlink()
    endpoint.require_active()
    _authorize_execution(local)


def _require_live(value: Identity, message: str) -> None:
    if resolve(value) is None:
        raise AuthorizationError(message)


def _acknowledge(
    path: Path, registration: Registration, owner: Identity, guardian: Identity
) -> None:
    """Authorize ACK publication at the last owner check, not permission to execute.

    Death during publication can briefly leave stale ACK bytes. The postcheck
    revokes them; the gate must independently check its owner before execution.
    """
    if registration.owner != owner or registration.guardian != guardian:
        raise AuthorizationError("Registration belongs to a different owner or guardian")
    _require_live(registration.process, "Registered process exited before acknowledgment")
    _require_live(owner, "Owner exited before registration acknowledgment")
    durable_write(path, _REGISTRATION.encode(registration).decode())
    try:
        _require_live(owner, "Owner exited during registration acknowledgment")
    except AuthorizationError:
        path.unlink(missing_ok=True)
        raise


def acknowledge_registered(
    path: Path,
    owned: set[Identity],
    endpoint: GuardianEndpoint,
    *,
    before_ack: Callable[[Registration], None] = _ignore_registration,
) -> None:
    registration = _REGISTRATION.decode(path.read_text())
    if registration.owner != endpoint.owner or registration.guardian != endpoint.guardian:
        raise AuthorizationError("Registration belongs to a different owner or guardian")
    endpoint.require_active()
    process = resolve(registration.process)
    if process is None:
        raise AuthorizationError("Cannot register an exited native process")
    if isinstance(registration.entry, VmEntry):
        launchers = ancestors(process.pid)[:1]
        if not launchers or launchers[0] != registration.entry.launcher:
            raise AuthorizationError("VM registration has a different actual launcher parent")
    parents = set(ancestors(process.pid))
    if registration.process not in owned and not parents.intersection(owned):
        raise AuthorizationError("Native process has no registered owner")
    owned.add(registration.process)
    record(endpoint.directory, owned, "tracking")
    before_ack(registration)
    ack = path.with_suffix(".ack")
    _acknowledge(ack, registration, endpoint.owner, endpoint.guardian)
    try:
        endpoint.require_active()
    except AuthorizationError:
        ack.unlink(missing_ok=True)
        raise
    path.unlink()


def _authorize_execution(registration: Registration) -> None:
    """Observe guardian liveness, then grant execution at the final owner birth check.

    These are separate OS observations, not an atomic lease. Death after a successful
    observation can race execv. Post-grant owner death requires owned cleanup and
    leaves effects uncertain; it cannot roll back instructions already authorized.
    """
    _require_live(registration.guardian, "Guardian exited before ownership transfer")
    _require_live(registration.owner, "Owner exited before execution authorization")


def spawn_owned(
    directory: Path,
    argv: tuple[str, ...],
    *,
    timeout: float = 5.0,
    entry: SpawnEntry = _SERVICE_ENTRY,
) -> Identity:
    if (directory / "registration.closed").exists() or (directory / "stop").exists():
        raise StopRequested("Local spawn admission is sealed")
    reply = directory / "spawn.reply.json"
    reply.unlink(missing_ok=True)
    durable_write(
        directory / "spawn.request.json",
        _SPAWN_REQUEST.encode(SpawnRequest(argv=argv, entry=entry)).decode(),
    )
    deadline = time.monotonic() + timeout
    while not reply.exists():
        if (directory / "registration.closed").exists() or (directory / "stop").exists():
            raise StopRequested("Local spawn admission is sealed")
        if time.monotonic() >= deadline:
            raise TimeoutError("Guardian did not acknowledge owned process startup")
        time.sleep(0.01)
    return _SPAWN_REPLY.decode(reply.read_text()).identity


def supervise(directory: Path, owner: Identity) -> None:
    owned: set[Identity] = set()
    guardian = identity(os.getpid())
    external = os.environ.get("PYBEAM_OUTER_GUARDIAN")
    endpoint = None if external is None else ENDPOINT.decode(external)
    launches: list[LauncherBirth] = []
    target = (
        _CensusTarget(
            endpoint,
            STARTUP_PLAN.decode((directory / "startup-plan.json").read_bytes()),
        )
        if endpoint is not None and isinstance(endpoint.stop_policy, ManagedStop)
        else _NoCensus()
    )

    def write_census(registry: Registry, forced: int = 0) -> None:
        match target:
            case _NoCensus():
                return
            case _CensusTarget(endpoint=borrowed, plan=plan):
                durable_write(
                    borrowed.directory / "managed-startup-census.json",
                    CENSUS.encode(
                        ManagedStartupCensus(
                            owner=owner,
                            guardian=guardian,
                            plan=plan,
                            startup=progress(plan, tuple(launches)),
                            registry=registry,
                            forced_cleanup_count=forced,
                        )
                    ).decode(),
                )
            case other:
                assert_never(other)

    def before_ack(registration: Registration) -> None:
        if isinstance(target, _CensusTarget):
            observe_vm(launches, registration)
            write_census(Registry(phase="tracking", owned=tuple(owned)))

    if endpoint is not None:
        register_borrowed(
            endpoint,
            Registration(
                process=guardian, owner=owner, guardian=guardian, generation=secrets.token_hex(16)
            ),
        )
    environment = dict(
        os.environ,
        PYBEAM_GUARDIAN_IDENTITY=IDENTITY.encode(guardian).decode(),
        PYBEAM_OWNER_IDENTITY=IDENTITY.encode(owner).decode(),
        PYBEAM_OWNER_DIRECTORY=str(directory),
    )
    children: list[subprocess.Popen[bytes]] = []
    streams = contextlib.ExitStack()
    deadline = time.monotonic() + 120
    record(directory, owned, "tracking")
    try:
        while resolve(owner) is not None and not (directory / "stop").exists():
            admission = RegistrationPhase.OPEN
            if endpoint is not None:
                match endpoint.stop_policy:
                    case ImmediateStop():
                        endpoint.require_active()
                    case ManagedStop():
                        endpoint.require_lifetime()
                        if endpoint.stopping():
                            admission = RegistrationPhase.SEALED
                            durable_write(directory / "registration.closed", "draining")
                    case other:
                        assert_never(other)
            if time.monotonic() > deadline:
                raise TimeoutError("Local cluster lifetime exceeded")
            request = directory / "spawn.request.json"
            if request.exists() and admission is RegistrationPhase.OPEN:
                command = _SPAWN_REQUEST.decode(request.read_text())
                _require_live(owner, "Owner exited before owned process startup")
                record(directory, owned, "spawning")
                index = str(len(children))
                stdout = streams.enter_context((directory / (index + ".stdout.log")).open("wb"))
                stderr_name = (
                    command.entry.alias if isinstance(command.entry, LauncherEntry) else index
                )
                stderr = streams.enter_context(
                    (directory / (stderr_name + ".stderr.log")).open("wb")
                )
                child = subprocess.Popen(  # noqa: S603
                    [sys.executable, "-m", "pybeam.runtime.guardian", "gate", *command.argv],
                    start_new_session=True,
                    stdout=stdout,
                    stderr=stderr,
                    env=environment,
                )
                children.append(child)
                value = identity(child.pid)
                owned.add(value)
                if isinstance(target, _CensusTarget) and isinstance(command.entry, LauncherEntry):
                    launches.append(PendingLauncher(alias=command.entry.alias, launcher=value))
                    write_census(Registry(phase="tracking", owned=tuple(owned)))
                record(directory, owned, "tracking")
                durable_write(
                    directory / "spawn.reply.json",
                    _SPAWN_REPLY.encode(SpawnReply(identity=value)).decode(),
                )
                request.unlink()
            for path in (
                directory.glob("register-*.json") if admission is RegistrationPhase.OPEN else ()
            ):
                acknowledge_registered(
                    path,
                    owned,
                    GuardianEndpoint(
                        directory=directory,
                        owner=owner,
                        guardian=guardian,
                        stop_file=directory / "stop",
                    ),
                    before_ack=before_ack,
                )
            for child in children:
                child.poll()
            previous = frozenset(owned)
            discover(owned)
            if owned != previous:
                record(directory, owned, "tracking")
            time.sleep(0.01)
    finally:
        # Popen groups remain exclusively ours even if identity registration failed.
        for child in children:
            if child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, 15)
        if (
            endpoint is not None
            and isinstance(endpoint.stop_policy, ManagedStop)
            and (directory / "stop").exists()
            and resolve(owner) is not None
        ):
            grace = time.monotonic() + endpoint.stop_policy.guardian_grace_seconds
            while any(resolve(value) is not None for value in owned):
                if resolve(owner) is None or time.monotonic() >= grace:
                    break
                for child in children:
                    child.poll()
                time.sleep(0.01)
        forced = sum(resolve(value) is not None for value in owned)
        survivors = cleanup(owned)
        from pybeam.runtime.process import _stop_owned

        for child in children:
            if child.poll() is None:
                _stop_owned(child, 1.0)
        survivors = tuple(value for value in owned if resolve(value) is not None)
        streams.close()
        (directory / "home" / ".erlang.cookie").unlink(missing_ok=True)
        record(directory, owned, "uncertain" if survivors else "confirmed")
        write_census(REGISTRY.decode((directory / "ownership.json").read_bytes()), forced)
        if endpoint is not None and isinstance(endpoint.stop_policy, ManagedStop):
            durable_write(
                directory / "guardian-exit.json",
                GUARDIAN_EXIT.encode(
                    GuardianExit(
                        registry=REGISTRY.decode((directory / "ownership.json").read_bytes()),
                        forced_cleanup_count=forced,
                    )
                ).decode(),
            )


def gate(command: list[str]) -> None:
    directory = Path(os.environ["PYBEAM_OWNER_DIRECTORY"])
    value = identity(os.getpid())
    owner_text = os.environ.get("PYBEAM_OWNER_IDENTITY")
    if owner_text is None:
        raise AuthorizationError("Missing owner identity; entry is sealed")
    owner = IDENTITY.decode(owner_text)
    guardian = IDENTITY.decode(os.environ["PYBEAM_GUARDIAN_IDENTITY"])
    external = os.environ.get("PYBEAM_OUTER_GUARDIAN")
    endpoint = None if external is None else ENDPOINT.decode(external)
    alias = os.environ.get("PYBEAM_NODE_ALIAS")
    entry: RegisteredEntry = ServiceEntry()
    if alias is not None and endpoint is not None and isinstance(endpoint.stop_policy, ManagedStop):
        try:
            launcher = identity(os.getppid())
        except ProcessLookupError as error:
            raise AuthorizationError("VM registration has no launcher parent") from error
        entry = VmEntry(alias=alias, launcher=launcher)
    authorization = Registration(
        process=value, owner=owner, guardian=guardian, generation=secrets.token_hex(16), entry=entry
    )
    _require_live(owner, "Owner exited before process registration")
    _require_live(guardian, "Guardian exited before ownership transfer")
    registration = directory / ("register-" + str(value.pid) + ".json")
    durable_write(registration, _REGISTRATION.encode(authorization).decode())
    deadline = time.monotonic() + 5
    ack = registration.with_suffix(".ack")
    while not ack.exists():
        _require_live(owner, "Owner exited while waiting for acknowledgment")
        _require_live(guardian, "Guardian exited before ownership transfer")
        if time.monotonic() > deadline:
            raise TimeoutError("No guardian ownership acknowledgment; entry is sealed")
        time.sleep(0.01)
    if _REGISTRATION.decode(ack.read_text()) != authorization:
        raise AuthorizationError("Ownership acknowledgment has a different identity or generation")
    ack.unlink()
    if endpoint is not None:
        register_borrowed(endpoint, authorization)
    _authorize_execution(authorization)
    os.execv(command[0], command)  # noqa: S606


if __name__ == "__main__":
    if sys.argv[1] == "gate":
        gate(sys.argv[2:])
    else:
        supervise(Path(sys.argv[1]), IDENTITY.decode(sys.argv[2]))
