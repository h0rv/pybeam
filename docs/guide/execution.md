# How Python work runs

A BEAM process is cheap: about 2.7 KB, preemptively scheduled, millions per machine. A Python interpreter is not. PyBEAM does not start an interpreter per process. Many BEAM processes share a bounded pool of Python contexts, and each context runs Python in the mode you choose.

## From a BEAM process to Python

![A BEAM process sends a call to a mailbox. A bounded pool of Python contexts takes calls. Each context runs as worker, owngil or isolated. The result returns as a message, or a crash signal goes to the supervisor.](assets/call-path.svg)

1. Your task or actor is a BEAM process. Calls wait in its mailbox until a context is free; a million callers become a queue, not a million interpreters.
2. A context owns one OS thread. Its mode decides which interpreter and GIL run your code.
3. The result comes back as a message. If the context dies, its supervisor gets a crash signal and starts a fresh one. Supervision restarts the context; it does not replay the call or keep its in-memory state.

## Choose a mode

Each mode trades startup, memory and call cost against parallelism and crash isolation. Free-threaded or not is the interpreter you install, CPython 3.14 or 3.14t, not a PyBEAM option.

| Mode | Python runs in | CPU work in parallel | Native crash kills | Start a context | Memory per context | Empty call p50/p99 |
| --- | --- | --- | --- | --- | --- | --- |
| `worker` · 3.14 | Main interpreter, one shared GIL | No | The whole VM | 1.3 ms | 15 KiB | 17/31 µs |
| `worker` · 3.14t | Main interpreter, no GIL | Yes | The whole VM | 1.5 ms | 21 KiB | 29/51 µs |
| `owngil` · 3.14 | A subinterpreter with its own GIL | Yes | The whole VM | 79 ms | 10.5 MiB | 14/23 µs |
| `isolated` · 3.14 | A child OS process | Yes | Only that child | 90 ms | 24 MiB | 76/146 µs |
| Separate nodes | Another BEAM VM | Yes | Only that node | Seconds | A whole VM | Network round trip |

Pick `worker` for I/O-bound async code and cheap calls. On 3.14t it also runs CPU work in parallel, if your C extensions support free threading. Pick `owngil` for parallel pure-Python CPU work on the conventional build; extensions must support subinterpreters, and values cross the boundary by copy. Pick `isolated` when a C extension can segfault or leak and you want only that call to fail. Add nodes when one machine is not enough or a VM failure must not take everything down.

Tasks choose a mode with `ExecutionMode`. Today they accept `ExecutionMode.WORKER`; `OWNGIL` and `ISOLATED` raise `CapabilityUnavailable` until the runtime accepts a per-task mode. Actors run in worker mode. `dedicated_loop=True` gives each worker task its own event loop; on 3.14t that is what lets CPU-bound tasks overlap, because tasks sharing one loop still take turns.

```python
from pydantic import TypeAdapter

from pybeam import run
from pybeam.tasks import ExecutionMode, TaskFunction, TaskLimits, TaskSupervisor


async def checksum(iterations: int) -> int:
    value = 0
    for index in range(iterations):
        value = (value * 31 + index) & 0xFFFFFFFF
    return value


async def main() -> None:
    job = TaskFunction(
        checksum,
        TypeAdapter(int),
        TypeAdapter(int),
        execution=ExecutionMode.WORKER,
        dedicated_loop=True,
    )
    limits = TaskLimits(capacity=4, max_bytes=4096, shutdown_ms=1000)
    async with TaskSupervisor(limits) as supervisor:
        tasks = [await supervisor.start(job, 3_000_000) for _ in range(4)]
        for task in tasks:
            print(await task)


if __name__ == "__main__":
    run(main)
```

It prints the same checksum four times. On 3.14 the four tasks take turns; on 3.14t they run at once.

## Fan out: four CPU jobs

Each row runs the same four pure-Python jobs at once, against today's Python tools. Bars show when each job ran.

![Four CPU jobs on a shared time axis for asyncio, threads on 3.14 and 3.14t, a process per job, a prefork pool, and PyBEAM worker, owngil, isolated and separate-node modes, with wall time and speedup per row.](assets/fan-out.svg)

On the GIL build, asyncio, threads and `worker` run the jobs one at a time. Free-threaded 3.14t threads and `worker` overlap them. `owngil`, `isolated`, a process per job, a prefork pool and separate nodes overlap them on every build, at the start and memory costs in the table above. The isolated · 3.14t row starts conventional GIL child interpreters here, so its parallelism comes from separate processes, not free threading.

## Blast radius: one job calls `os.abort()`

Four jobs run; one aborts the interpreter.

| Lane | Other jobs that finish | Replacement ready in |
| --- | --- | --- |
| asyncio, threads | 0 of 3: the process dies | Restart the app |
| `worker`, `owngil` | 0 of 3: the VM dies | Restart the app |
| Process per job | 3 of 3 | 80 ms for a new process |
| Prefork pool | 0 of 3: the pool breaks | 53 ms for a new pool |
| `isolated` | 3 of 3 | 70 ms for a new child |
| Separate nodes | 3 of 3 | 254 ms for a new node |

Subinterpreters isolate Python state, not native crashes. Only a separate OS process keeps a segfault away from everything else.

## Fan in: 100,000 callers

100,000 callers each make one empty call.

| Lane | Contexts | Memory per caller | Start all callers | Finish all calls |
| --- | --- | --- | --- | --- |
| asyncio tasks | 1 event loop | 1.1 KB | 463 ms | 429 ms |
| `worker` · 3.14 | 8, at most 32 calls admitted | 2.7 KB | 232 ms | 2.59 s |
| `worker` · 3.14t | 8, at most 32 calls admitted | 2.7 KB | 258 ms | 1.80 s |

For no-op calls on one machine, asyncio is cheaper and faster. Callers here are BEAM processes queued in front of eight contexts: each one is preemptively scheduled, can be supervised, and can live on another node, and the admission window bounds how much Python work is in flight.

## Run it yourself

These numbers come from one 8-core Linux x86_64 machine and change with hardware and load. They are not production benchmarks. From a repository checkout with the native runtime built, run the lab and repeat each experiment live:

```sh
mise run modes-lab
```

Open http://127.0.0.1:38941/. The lab drives erlang-python contexts directly, so it can show `owngil` and `isolated` before tasks accept them. Free-threaded lanes need a 3.14t runtime; set `PYBEAM_LAB_PYTHON_314T` and `PYBEAM_LAB_ERLANG_PYTHON_314T` to point at it.
