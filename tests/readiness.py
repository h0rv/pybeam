import os
import selectors
import subprocess
import time
from collections.abc import Callable
from typing import Protocol

STARTUP_SECONDS = 10.0
MAX_OUTPUT_BYTES = 65_536
READY_LINE = b"SHUTDOWN_READY\n"


class ReadinessSource(Protocol):
    def poll(self) -> int | None: ...
    def read(self, timeout: float) -> bytes | None: ...


class ReadinessFailure(AssertionError):
    pass


def wait_ready(
    source: ReadinessSource,
    output: bytearray,
    *,
    clock: Callable[[], float] = time.monotonic,
    ready_line: bytes = READY_LINE,
) -> None:
    deadline = clock() + STARTUP_SECONDS

    def failure(reason: str) -> ReadinessFailure:
        return ReadinessFailure(reason + "\n" + output.decode(errors="replace"))

    def append(chunk: bytes) -> None:
        remaining = MAX_OUTPUT_BYTES - len(output)
        output.extend(chunk[:remaining])
        if len(chunk) > remaining:
            raise failure("Readiness output exceeded 65536 bytes")

    while True:
        status = source.poll()
        if status is not None:
            while True:
                chunk = source.read(0)
                if chunk is None or chunk == b"":
                    break
                append(chunk)
            raise failure("Child exited before readiness, status=" + str(status))
        remaining = deadline - clock()
        if remaining <= 0:
            raise failure("Readiness deadline exceeded after 10 seconds")
        chunk = source.read(min(1.0, remaining))
        if chunk is None:
            continue
        if not chunk:
            raise failure("EOF before readiness, child status=" + str(source.poll()))
        append(chunk)
        if clock() >= deadline:
            raise failure("Readiness deadline exceeded after 10 seconds")
        if ready_line in output:
            if source.poll() is not None:
                raise failure("Child exited at readiness")
            return


class ProcessOutput:
    def __init__(self, child: subprocess.Popen[bytes], selector: selectors.BaseSelector) -> None:
        if child.stdout is None:
            raise ValueError("The child requires a captured combined stdout/stderr pipe")
        self.child = child
        self.selector = selector
        self.descriptor = child.stdout.fileno()
        selector.register(child.stdout, selectors.EVENT_READ)

    def poll(self) -> int | None:
        return self.child.poll()

    def read(self, timeout: float) -> bytes | None:
        if not self.selector.select(timeout=timeout):
            return None
        return os.read(self.descriptor, 4096)
