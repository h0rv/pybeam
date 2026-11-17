import os
import sys
import threading
import time
from concurrent import interpreters


def spin(micros: int) -> dict[str, int | bool]:
    start = time.monotonic_ns()
    time.sleep(micros / 1_000_000)
    return {
        "start": start,
        "end": time.monotonic_ns(),
        "thread": threading.get_native_id(),
        "pid": os.getpid(),
        "interpreter": interpreters.get_current().id,
        "gil": sys._is_gil_enabled(),
    }


def noop() -> int:
    return 0
