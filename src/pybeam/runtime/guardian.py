import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

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
    os.environ["PYBEAM_GUARDIAN_IDENTITY"] = identity(psutil.Process()).model_dump_json()
    children: list[subprocess.Popen[bytes]] = []
    streams = contextlib.ExitStack()
    deadline = time.monotonic() + 120
    record(directory, owned, "tracking")
    try:
        while resolve(owner) is not None and not (directory / "stop").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Local cluster lifetime exceeded")
            request = directory / "spawn.request.json"
            if request.exists():
                command = SpawnRequest.model_validate_json(request.read_text())
                record(directory, owned, "spawning")
                index = str(len(children))
                stdout = streams.enter_context((directory / (index + ".stdout.log")).open("wb"))
                stderr = streams.enter_context((directory / (index + ".stderr.log")).open("wb"))
                child = subprocess.Popen(  # noqa: S603
                    [sys.executable, "-m", "pybeam.runtime.guardian", "gate", *command.argv],
                    start_new_session=True,
                    stdout=stdout,
                    stderr=stderr,
                )
                children.append(child)
                value = identity(psutil.Process(child.pid))
                owned.add(value)
                record(directory, owned, "tracking")
                durable_write(
                    directory / "spawn.reply.json", SpawnReply(identity=value).model_dump_json()
                )
                request.unlink()
            for registration in directory.glob("register-*.json"):
                value = Identity.model_validate_json(registration.read_text())
                process = resolve(value)
                if process is None:
                    raise RuntimeError("Cannot register an exited native process")
                ancestors = {identity(parent) for parent in process.parents() if parent.pid > 0}
                if value not in owned and not ancestors.intersection(owned):
                    raise RuntimeError("Native process has no registered owner")
                owned.add(value)
                record(directory, owned, "tracking")
                durable_write(registration.with_suffix(".ack"), value.model_dump_json())
                registration.unlink()
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
    registration = directory / ("register-" + str(value.pid) + ".json")
    durable_write(registration, value.model_dump_json())
    deadline = time.monotonic() + 5
    ack = registration.with_suffix(".ack")
    while not ack.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("No guardian ownership acknowledgment; entry is sealed")
        time.sleep(0.01)
    if Identity.model_validate_json(ack.read_text()) != value:
        raise RuntimeError("Ownership acknowledgment belongs to a different process")
    ack.unlink()
    guardian = Identity.model_validate_json(os.environ["PYBEAM_GUARDIAN_IDENTITY"])
    if resolve(guardian) is None:
        raise RuntimeError("Guardian exited before ownership transfer")
    os.execv(command[0], command)  # noqa: S606


if __name__ == "__main__":
    if sys.argv[1] == "gate":
        gate(sys.argv[2:])
    else:
        supervise(Path(sys.argv[1]), Identity.model_validate_json(sys.argv[2]))
