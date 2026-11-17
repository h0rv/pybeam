# How Python work runs

A BEAM process starts at about 2.7 KB ([Erlang memory guide](https://www.erlang.org/doc/system/memory.html)), so you can run millions. A Python interpreter costs far more, so PyBEAM does not start one per process. Each task or actor gets a Python context, limits cap how many run at once, and you choose how each context runs Python.

## From a BEAM process to Python

![A BEAM process sends a call to a mailbox. A Python context takes the call. Supported contexts run as worker or isolated. The result returns as a message, or a crash signal goes to the supervisor.](assets/call-path.svg)

1. Your task or actor is a BEAM process. Calls wait in its mailbox until its context is free. `TaskLimits` or an actor group's `max_children` caps how many contexts exist; starting a task when its ownership slots are full raises `Overloaded` instead of queuing it.
2. A context owns one OS thread. Its mode decides which interpreter and GIL run your code.
3. The result comes back as a message. If an actor crashes, its group starts a replacement with fresh state; it does not replay the call. Tasks are temporary children: their supervisor does not restart them.

## Choose a mode

Use free-threaded CPython 3.14t workers by default. Use isolated OS processes for code that might crash the interpreter. Start and memory are per context, and call is an empty round trip.

**Own-GIL subinterpreter mode (`owngil`) is unsupported:** common native extensions refuse to load in subinterpreters on both CPython 3.14 and 3.14t. Free-threading support does not imply subinterpreter support. See the [package compatibility results](../research/execution-modes.md#unsupported-own-gil-subinterpreters).

| Mode | Parallel CPU | Crash kills | Start | Memory | Call p50/p99 |
| --- | --- | --- | --- | --- | --- |
| `worker` · 3.14 | No | The VM | 1.3 ms | 15 KiB | 17/31 µs |
| `worker` · 3.14t | Yes | The VM | 1.5 ms | 21 KiB | 29/51 µs |
| `isolated` · 3.14 | Yes | That child | 90 ms | 24 MiB | 76/146 µs |
| Separate nodes | Yes | That node | Seconds | A whole VM | Network |

- `worker`: I/O-bound async code and cheap calls. On 3.14t it also runs CPU work in parallel, if your C extensions support free threading.
- `isolated`: C extensions that can crash or leak. Only that call fails.
- Separate nodes: surviving a whole VM failure.

Supported task modes are `worker` and `isolated`; actors remain in `worker`. `dedicated_loop=True` gives each `worker` task its own event loop and thread, so CPU-bound tasks do not wait on each other's loop.

```python
# execution.py
from pybeam import run
from pybeam.codec import Json
from pybeam.tasks import (
    ExecutionMode,
    TaskFunction,
    TaskLimits,
    TaskSupervisor,
)


async def checksum(iterations: int) -> int:
    value = 0
    for index in range(iterations):
        value = (value * 31 + index) & 0xFFFFFFFF
    return value


async def main() -> None:
    job = TaskFunction(
        checksum,
        Json(int),
        Json(int),
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

```console
$ uv run execution.py
637018976
637018976
637018976
637018976
```

On 3.14 the four tasks take turns. On 3.14t they run on four threads at once; in a local run four took 0.43 s against 0.32 s for one. Threads that touch the same Python object contend for it on 3.14t, so keep hot objects per task, and time your own workload before you count on parallel tasks.

## Fan out: four CPU jobs

Each row runs the same four pure-Python jobs at once. Bars show when each job ran.

![Four CPU jobs on a shared time axis for asyncio, threads on 3.14 and 3.14t, a process per job, a prefork pool, and PyBEAM worker, isolated and separate-node modes, with wall time and speedup per row.](assets/fan-out.svg)

With the GIL, asyncio, threads and `worker` run the jobs one at a time. On 3.14t, threads and `worker` overlap them. `isolated`, a process per job, a prefork pool and separate nodes overlap them on either build, at the costs in the table above.

## Blast radius: one job calls `os.abort()`

Four jobs run; one aborts the interpreter.

| Lane | Other jobs that finish | Replacement ready in |
| --- | --- | --- |
| asyncio, threads | 0 of 3: the process dies | Restart the app |
| `worker` | 0 of 3: the VM dies | Restart the app |
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

Asyncio is fast and simple, but each event loop lives in one Python process, often inside a container; its tasks are isolated from other processes and lost if that process crashes. PyBEAM callers are preemptively scheduled and supervised. The admission limit bounds how much Python work runs at once.

Numbers on this page come from one 8-core machine and vary with hardware and load.

## Inspect a running app

Start your app with debugging enabled, then open the monitor in another terminal:

```sh
pybeam run --debug app.py
```

```sh
pybeam tui
```

Select an app and press Enter. The monitor refreshes actor mailboxes and admitted
calls, task ownership slots, and queue depth and active leases every two seconds.
Full means a limit has been reached, not that the app has crashed. Task slots can
remain occupied until their results are observed. Counts are live samples, not an
atomic snapshot or an application-level health check.

Press Enter on a resource for its counters and the Python stacks, or `p` for a
Python shell in the app's globals. Stacks show all worker threads and entry-loop
tasks, not a stack matched to the selected resource; isolated child interpreters
are not visible. Press `b` to go back and `q` to detach without
stopping the app. `pybeam tui <name>` attaches directly; `pybeam ps` lists names.

Debugging is opt-in and local to your user account. The shell can change the app's
state, and statements print to the app's terminal. Keep it for trusted development
sessions; it is not a read-only production interface.
