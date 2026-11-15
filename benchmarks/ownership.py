import os
import time
from pathlib import Path

import psutil
from pydantic import Field

from benchmarks.contract import Identity, StrictModel


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
