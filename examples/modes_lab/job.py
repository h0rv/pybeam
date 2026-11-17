import os
import sys
import threading
import time
from concurrent import interpreters


def spin(iterations: int) -> dict[str, int | bool]:
    start = time.monotonic_ns()
    value = 0
    for index in range(iterations):
        value = (value * 31 + index) & 0xFFFFFFFF
    return {
        "start": start,
        "end": time.monotonic_ns(),
        "thread": threading.get_native_id(),
        "pid": os.getpid(),
        "interpreter": interpreters.get_current().id,
        "gil": sys._is_gil_enabled(),
        "value": value,
    }


def noop() -> int:
    return 0


def crash() -> None:
    os.abort()
