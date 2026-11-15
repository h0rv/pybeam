import contextlib
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import psutil
from pydantic import BaseModel, ConfigDict, Field

from pybeam.runtime.ownership import (
    Identity,
    cleanup,
    discover,
    durable_write,
    identity,
    record,
    resolve,
)


class SpawnRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    argv: tuple[str, ...] = Field(min_length=1)


class SpawnReply(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    identity: Identity


class AuthorizationError(RuntimeError):
    pass


class Registration(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    process: Identity
    owner: Identity
    guardian: Identity
    generation: Annotated[str, Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]


class GuardianEndpoint(BaseModel):
    """Borrow an existing local guardian's registration directory and lifetime.

    The borrower never starts or stops this guardian. Its owner must durably
    record and acknowledge each birth before any nested entry is authorized.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    directory: Path
    owner: Identity
    guardian: Identity
    stop_file: Path | None = None
    control_owner: Identity | None = None

    def require_active(self) -> None:
        _require_live(self.owner, "Outer owner exited before execution authorization")
        _require_live(self.guardian, "Outer guardian exited before execution authorization")
        if self.control_owner is not None:
            _require_live(self.control_owner, "Outer control owner exited before authorization")
        if (self.directory / "registration.closed").exists() or (
            self.stop_file is not None and self.stop_file.exists()
        ):
            raise AuthorizationError("Outer owner cancelled execution authorization")


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
    durable_write(path, registration.model_dump_json())
    ack = path.with_suffix(".ack")
    deadline = time.monotonic() + 5
    while not ack.exists():
        endpoint.require_active()
        _authorize_execution(local)
        if time.monotonic() > deadline:
            raise AuthorizationError("No outer guardian acknowledgment; entry is sealed")
        time.sleep(0.01)
    if Registration.model_validate_json(ack.read_text()) != registration:
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
    durable_write(path, registration.model_dump_json())
    try:
        _require_live(owner, "Owner exited during registration acknowledgment")
    except AuthorizationError:
        path.unlink(missing_ok=True)
        raise


def acknowledge_registered(path: Path, owned: set[Identity], endpoint: GuardianEndpoint) -> None:
    registration = Registration.model_validate_json(path.read_text())
    if registration.owner != endpoint.owner or registration.guardian != endpoint.guardian:
        raise AuthorizationError("Registration belongs to a different owner or guardian")
    endpoint.require_active()
    process = resolve(registration.process)
    if process is None:
        raise AuthorizationError("Cannot register an exited native process")
    ancestors = {identity(parent) for parent in process.parents() if parent.pid > 0}
    if registration.process not in owned and not ancestors.intersection(owned):
        raise AuthorizationError("Native process has no registered owner")
    owned.add(registration.process)
    record(endpoint.directory, owned, "tracking")
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


def spawn_owned(directory: Path, argv: tuple[str, ...], *, timeout: float = 5.0) -> Identity:
    reply = directory / "spawn.reply.json"
    reply.unlink(missing_ok=True)
    durable_write(directory / "spawn.request.json", SpawnRequest(argv=argv).model_dump_json())
    deadline = time.monotonic() + timeout
    while not reply.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Guardian did not acknowledge owned process startup")
        time.sleep(0.01)
    return SpawnReply.model_validate_json(reply.read_text()).identity


def supervise(directory: Path, owner: Identity) -> None:
    owned: set[Identity] = set()
    guardian = identity(psutil.Process())
    external = os.environ.get("PYBEAM_OUTER_GUARDIAN")
    endpoint = None if external is None else GuardianEndpoint.model_validate_json(external)
    if endpoint is not None:
        register_borrowed(
            endpoint,
            Registration(
                process=guardian, owner=owner, guardian=guardian, generation=secrets.token_hex(16)
            ),
        )
    environment = dict(
        os.environ,
        PYBEAM_GUARDIAN_IDENTITY=guardian.model_dump_json(),
        PYBEAM_OWNER_IDENTITY=owner.model_dump_json(),
        PYBEAM_OWNER_DIRECTORY=str(directory),
    )
    children: list[subprocess.Popen[bytes]] = []
    streams = contextlib.ExitStack()
    deadline = time.monotonic() + 120
    record(directory, owned, "tracking")
    try:
        while resolve(owner) is not None and not (directory / "stop").exists():
            if endpoint is not None:
                endpoint.require_active()
            if time.monotonic() > deadline:
                raise TimeoutError("Local cluster lifetime exceeded")
            request = directory / "spawn.request.json"
            if request.exists():
                command = SpawnRequest.model_validate_json(request.read_text())
                _require_live(owner, "Owner exited before owned process startup")
                record(directory, owned, "spawning")
                index = str(len(children))
                stdout = streams.enter_context((directory / (index + ".stdout.log")).open("wb"))
                stderr = streams.enter_context((directory / (index + ".stderr.log")).open("wb"))
                child = subprocess.Popen(  # noqa: S603
                    [sys.executable, "-m", "pybeam.runtime.guardian", "gate", *command.argv],
                    start_new_session=True,
                    stdout=stdout,
                    stderr=stderr,
                    env=environment,
                )
                children.append(child)
                value = identity(psutil.Process(child.pid))
                owned.add(value)
                record(directory, owned, "tracking")
                durable_write(
                    directory / "spawn.reply.json", SpawnReply(identity=value).model_dump_json()
                )
                request.unlink()
            for path in directory.glob("register-*.json"):
                acknowledge_registered(
                    path,
                    owned,
                    GuardianEndpoint(
                        directory=directory,
                        owner=owner,
                        guardian=guardian,
                        stop_file=directory / "stop",
                    ),
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
        survivors = cleanup(owned)
        from pybeam.runtime.process import _stop_owned

        for child in children:
            if child.poll() is None:
                _stop_owned(child, 1.0)
        survivors = tuple(value for value in owned if resolve(value) is not None)
        streams.close()
        (directory / "home" / ".erlang.cookie").unlink(missing_ok=True)
        record(directory, owned, "uncertain" if survivors else "confirmed")


def gate(command: list[str]) -> None:
    directory = Path(os.environ["PYBEAM_OWNER_DIRECTORY"])
    value = identity(psutil.Process())
    owner_text = os.environ.get("PYBEAM_OWNER_IDENTITY")
    if owner_text is None:
        raise AuthorizationError("Missing owner identity; entry is sealed")
    owner = Identity.model_validate_json(owner_text)
    guardian = Identity.model_validate_json(os.environ["PYBEAM_GUARDIAN_IDENTITY"])
    authorization = Registration(
        process=value, owner=owner, guardian=guardian, generation=secrets.token_hex(16)
    )
    _require_live(owner, "Owner exited before process registration")
    _require_live(guardian, "Guardian exited before ownership transfer")
    registration = directory / ("register-" + str(value.pid) + ".json")
    durable_write(registration, authorization.model_dump_json())
    deadline = time.monotonic() + 5
    ack = registration.with_suffix(".ack")
    while not ack.exists():
        _require_live(owner, "Owner exited while waiting for acknowledgment")
        _require_live(guardian, "Guardian exited before ownership transfer")
        if time.monotonic() > deadline:
            raise TimeoutError("No guardian ownership acknowledgment; entry is sealed")
        time.sleep(0.01)
    if Registration.model_validate_json(ack.read_text()) != authorization:
        raise AuthorizationError("Ownership acknowledgment has a different identity or generation")
    ack.unlink()
    external = os.environ.get("PYBEAM_OUTER_GUARDIAN")
    if external is not None:
        register_borrowed(GuardianEndpoint.model_validate_json(external), authorization)
    _authorize_execution(authorization)
    os.execv(command[0], command)  # noqa: S606


if __name__ == "__main__":
    if sys.argv[1] == "gate":
        gate(sys.argv[2:])
    else:
        supervise(Path(sys.argv[1]), Identity.model_validate_json(sys.argv[2]))
