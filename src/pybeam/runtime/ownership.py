import contextlib
import os
import time
from pathlib import Path
from typing import Annotated, Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field


class Identity(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    pid: Annotated[int, Field(gt=0)]
    created: Annotated[float, Field(gt=0, allow_inf_nan=False)]


class Registry(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

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
