# Blocking and CPU work

Tasks share one event loop by default. While one of them runs a blocking call or a long CPU loop, every other task on that loop waits, just as in plain asyncio. Actor calls use a bounded pool of Python workers with separate loops. A blocking method occupies one worker; calls queue when all workers are busy.

- Wrap a blocking call in `await asyncio.to_thread(...)`. The default executor runs a few calls at once, so many blocking calls still queue.
- Give a CPU-bound task its own loop with `dedicated_loop=True`.
- Run code that might crash the process, such as an unreliable C extension, with `execution=ExecutionMode.ISOLATED`. Isolated tasks run in separate Python processes; their functions must be importable top-level `async def` functions that take and return `bytes`. A child crash raises `TaskCrashed` and is not retried. External effects remain unknown. Process isolation adds startup and memory overhead; it is not a security sandbox.

Actors have no `dedicated_loop` option and need none: each call already runs on a pooled worker. Parallel Python CPU work requires free-threaded Python. Don't keep loop-bound objects such as `asyncio.Lock` in actor state across calls, because the next call may run on another worker. Create and finalize those resources within one call. Closing a group releases actor state but leaves the shared pool running.

This example runs four CPU-bound tasks, each on its own loop:

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

On standard Python 3.14 the four CPU tasks take turns. Free-threaded Python can run them in parallel when dependencies support it. Keep mutable data inside each task rather than in shared module globals. Time your own workload before relying on a speedup.

For how this works and the measurements behind it, see [How PyBEAM works](internals/index.md).

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
Python shell in the app's globals. Stacks show all threads and entry-loop tasks,
not a stack matched to the selected resource; isolated tasks are not visible.
Press `b` to go back and `q` to detach without stopping the app. `pybeam tui <name>` attaches directly; `pybeam ps` lists names.

Debugging is opt-in and local to your user account. The shell can change the app's
state, and statements print to the app's terminal. Keep it for trusted development
sessions; it is not a read-only production interface.
