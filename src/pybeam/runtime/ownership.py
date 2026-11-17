import contextlib
import ctypes
import math
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pybeam.codec import Json


@dataclass(frozen=True, slots=True)
class Identity:
    """A process ID plus its start time, so a reused PID never matches."""

    pid: int
    created: float

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("pid must be a positive integer")
        if not math.isfinite(self.created) or self.created <= 0:
            raise ValueError("created must be a positive finite number")


type Phase = Literal["tracking", "spawning", "confirmed", "uncertain"]


@dataclass(frozen=True, slots=True)
class Registry:
    phase: Phase
    owned: tuple[Identity, ...]


IDENTITY = Json(Identity)
REGISTRY = Json(Registry)


@dataclass(frozen=True, slots=True)
class Process:
    pid: int
    parent: int
    created: float
    zombie: bool


def _linux_boot() -> float:
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise RuntimeError("/proc/stat has no boot time")


def _linux(pid: int) -> Process | None:
    try:
        data = Path("/proc/" + str(pid) + "/stat").read_bytes()
    except FileNotFoundError, ProcessLookupError:
        return None
    # The command name is parenthesized, arbitrary bytes, and may contain spaces or parentheses.
    fields = data[data.rindex(b")") + 2 :].split()
    ticks = int(fields[19]) / os.sysconf("SC_CLK_TCK")
    return Process(pid, int(fields[1]), _linux_boot() + ticks, fields[0] in {b"Z", b"X"})


class _BsdInfo(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("ids", ctypes.c_uint32 * 6),
        ("rfu", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    )


_BSD_INFO = 3
_ZOMBIE = 5


def _darwin(pid: int) -> Process | None:
    # libproc reads the kernel process table without spawning a child process.
    info = _BsdInfo()
    size = ctypes.sizeof(info)
    if _LIBPROC.proc_pidinfo(pid, _BSD_INFO, 0, ctypes.byref(info), size) != size:
        return None
    created = info.start_sec + info.start_usec / 1_000_000
    return Process(pid, info.ppid, created, info.status == _ZOMBIE)


def _darwin_pids() -> tuple[int, ...]:
    count = int(_LIBPROC.proc_listallpids(None, 0))
    buffer = (ctypes.c_int * (count + 64))()
    found = int(_LIBPROC.proc_listallpids(buffer, ctypes.sizeof(buffer)))
    return tuple(int(pid) for pid in buffer[: max(found, 0)] if pid > 0)


if sys.platform == "darwin":
    _LIBPROC = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    _LIBPROC.proc_pidinfo.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    )
    _LIBPROC.proc_pidinfo.restype = ctypes.c_int
    _LIBPROC.proc_listallpids.argtypes = (ctypes.c_void_p, ctypes.c_int)
    _LIBPROC.proc_listallpids.restype = ctypes.c_int


def _process(pid: int) -> Process | None:
    if sys.platform == "linux":
        return _linux(pid)
    if sys.platform == "darwin":
        return _darwin(pid)
    raise OSError("Process inspection supports Linux and macOS")


def _table() -> tuple[Process, ...]:
    if sys.platform == "linux":
        pids = tuple(int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit())
    elif sys.platform == "darwin":
        pids = _darwin_pids()
    else:
        raise OSError("Process inspection supports Linux and macOS")
    found = (_process(pid) for pid in pids)
    return tuple(item for item in found if item is not None)


def identity(pid: int) -> Identity:
    """Identify a live process. Raises ProcessLookupError if it has exited."""
    process = _process(pid)
    if process is None:
        raise ProcessLookupError(pid)
    return Identity(pid, process.created)


def resolve(value: Identity) -> Process | None:
    process = _process(value.pid)
    if process is None or process.created != value.created or process.zombie:
        return None
    return process


def _descendants(pids: set[int]) -> tuple[Process, ...]:
    children: dict[int, list[Process]] = {}
    for item in _table():
        children.setdefault(item.parent, []).append(item)
    found: list[Process] = []
    pending = list(pids)
    while pending:
        for child in children.get(pending.pop(), ()):
            if child.pid not in pids:
                pids.add(child.pid)
                found.append(child)
                pending.append(child.pid)
    return tuple(found)


def descendants(pid: int) -> tuple[Identity, ...]:
    """Identify every live descendant of a process."""
    return tuple(
        Identity(item.pid, item.created) for item in _descendants({pid}) if not item.zombie
    )


def ancestors(pid: int) -> tuple[Identity, ...]:
    """Identify the live parents of a process, nearest first."""
    found: list[Identity] = []
    process = _process(pid)
    while process is not None and process.parent > 0 and len(found) < 4096:
        process = _process(process.parent)
        if process is not None:
            found.append(Identity(process.pid, process.created))
    return tuple(found)


def _signal(value: Identity, number: signal.Signals) -> None:
    if resolve(value) is not None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(value.pid, number)


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
    phase: Phase,
) -> None:
    durable_write(
        directory / "ownership.json", REGISTRY.encode(Registry(phase, tuple(owned))).decode()
    )


def discover(owned: set[Identity]) -> None:
    live = {value.pid for value in owned if resolve(value) is not None}
    owned.update(Identity(item.pid, item.created) for item in _descendants(live) if not item.zombie)


def cleanup(owned: set[Identity]) -> tuple[Identity, ...]:
    discover(owned)
    for value in owned:
        _signal(value, signal.SIGSTOP)
    discover(owned)
    for value in owned:
        _signal(value, signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while any(resolve(value) is not None for value in owned) and time.monotonic() < deadline:
        time.sleep(0.01)
    return tuple(value for value in owned if resolve(value) is not None)
