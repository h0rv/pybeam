import json
import marshal
import pickle
import socket
import statistics
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Order:
    id: int
    items: list[str]
    total: float


def state(entries: int) -> dict[str, object]:
    return {"key" + str(index): "value" * 4 for index in range(entries)} | {"count": 0}


def median_us(operation: Callable[[], object]) -> float:
    operation()
    samples = []
    for _ in range(200):
        start = time.perf_counter_ns()
        operation()
        samples.append(time.perf_counter_ns() - start)
    return round(statistics.median(samples) / 1000, 1)


def main() -> None:
    build = "3.14t" if not sys._is_gil_enabled() else "3.14"
    for entries in (32, 3300, 33000):
        value = state(entries)
        row: dict[str, object] = {"build": build, "entries": entries}
        for name, dumps, loads in (
            ("pickle", pickle.dumps, pickle.loads),
            ("marshal", marshal.dumps, marshal.loads),
            ("json", lambda v: json.dumps(v).encode(), json.loads),
        ):
            blob = dumps(value)
            row[name + "_bytes"] = len(blob)
            row[name + "_roundtrip_us"] = median_us(
                lambda b=blob, encode=dumps, decode=loads: encode(decode(b))
            )
        print(json.dumps(row), flush=True)
    holds = {
        "dataclass": Order(1, ["a"], 2.5),
        "lambda": lambda: 0,
        "lock": threading.Lock(),
        "socket": socket.socket(),
        "generator": (n for n in range(2)),
    }
    for name, value in holds.items():
        results = {}
        for codec, dumps in (
            ("pickle", pickle.dumps),
            ("marshal", marshal.dumps),
            ("json", json.dumps),
        ):
            try:
                dumps(value)
                results[codec] = "ok"
            except (TypeError, ValueError, pickle.PicklingError, AttributeError) as error:
                results[codec] = type(error).__name__
        print(json.dumps({"build": build, "holds": name, **results}), flush=True)


if __name__ == "__main__":
    main()
