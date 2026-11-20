import os
from statistics import median
from time import perf_counter

import isolated_cost as entries

import pybeam


async def identify(payload: bytes) -> bytes:
    return str(os.getpid()).encode()


async def main() -> None:
    durations: list[float] = []
    children: set[bytes] = set()
    for _ in range(10):
        started = perf_counter()
        children.add(await pybeam.isolated(entries.identify, b""))
        durations.append(perf_counter() - started)
    print("Median of 10 calls:", format(median(durations) * 1000, ".1f"), "ms")
    print("Distinct child PIDs:", len(children))


if __name__ == "__main__":
    pybeam.run(main)
