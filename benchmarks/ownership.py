import contextlib
import os
import time
from pathlib import Path
from typing import Literal

import psutil
from pydantic import Field

from benchmarks.contract import Identity, StrictModel


class Lease(StrictModel):
    trial_directory: str


def write_lease(descriptor: int, lease: Lease | None) -> None:
    payload = b"" if lease is None else lease.model_dump_json().encode()
    os.lseek(descriptor, 0, os.SEEK_SET)
    written = os.write(descriptor, payload)
    if written != len(payload):
        raise OSError("Incomplete load lease write; ownership must be inspected")
    os.ftruncate(descriptor, len(payload))
    os.fsync(descriptor)


class Registry(StrictModel):
    phase: Literal["tracking", "spawning", "confirmed", "uncertain"]
    owned: tuple[Identity, ...]


def durable_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("w") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def record(
    directory: Path,
    owned: set[Identity],
    phase: Literal["tracking", "spawning", "confirmed", "uncertain"],
) -> None:
    durable_write(
        directory / "ownership.json", Registry(phase=phase, owned=tuple(owned)).model_dump_json()
    )


class Control(StrictModel):
    owner: Identity
    stop_file: str


class SpawnRequest(StrictModel):
    argv: tuple[str, ...] = Field(min_length=1)


class SpawnReply(StrictModel):
    identity: Identity


def identity(process: psutil.Process) -> Identity:
    return Identity(pid=process.pid, created=process.create_time())


def resolve(value: Identity) -> psutil.Process | None:
    try:
        process = psutil.Process(value.pid)
        if process.create_time() == value.created and process.status() != psutil.STATUS_ZOMBIE:
            return process
    except psutil.NoSuchProcess:
        return None
    return None


def discover(owned: set[Identity]) -> None:
    for value in tuple(owned):
        process = resolve(value)
        if process is None:
            continue
        with contextlib.suppress(psutil.NoSuchProcess):
            owned.update(identity(child) for child in process.children(recursive=True))


def cleanup(owned: set[Identity]) -> tuple[Identity, ...]:
    discover(owned)
    for value in owned:
        process = resolve(value)
        if process is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                process.suspend()
    discover(owned)
    for value in owned:
        process = resolve(value)
        if process is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                process.kill()
    deadline = time.monotonic() + 1.0
    while any(resolve(value) is not None for value in owned) and time.monotonic() < deadline:
        time.sleep(0.01)
    return tuple(value for value in owned if resolve(value) is not None)


def spawn_owned(directory: Path, argv: tuple[str, ...], *, timeout: float = 5.0) -> Identity:
    """Ask the independent guardian to own a detached VM before replying.

    Use during adapter setup, serially. Do not launch detached grandchildren from
    the adapter itself. A managed launcher that cannot use this ownership path
    must report unsupported rather than claiming guaranteed orphan cleanup.
    """
    request = directory / "spawn.request.json"
    reply = directory / "spawn.reply.json"
    reply.unlink(missing_ok=True)
    temporary = directory / "spawn.pending.json"
    temporary.write_text(SpawnRequest(argv=argv).model_dump_json())
    os.replace(temporary, request)
    deadline = time.monotonic() + timeout
    while not reply.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Guardian did not acknowledge owned process startup")
        time.sleep(0.01)
    return SpawnReply.model_validate_json(reply.read_text()).identity
