# Blocking and CPU work

Tasks share one event loop by default. While one of them runs a blocking call or a long CPU loop, every other task on that loop waits, just as in plain asyncio. Actor calls use a bounded pool of Python workers with separate loops. A blocking method occupies one worker; calls queue when all workers are busy.

- Wrap a blocking call in `await asyncio.to_thread(...)`. The default executor runs a few calls at once, so many blocking calls still queue.
- Give a CPU-bound task its own loop with `dedicated_loop=True`.
- Run code that might crash the process, such as an unreliable C extension, with [`pybeam.isolated`](#isolated-calls) or `execution=ExecutionMode.ISOLATED`. Each isolated task runs in a separate Python process; its function must be an importable top-level `async def` that takes and returns `bytes`. A child crash raises `TaskCrashed`. Execution runs once, with unknown external effects after a crash. Process isolation adds startup and memory overhead; application permissions determine access to the host.

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

## Isolated calls

`await pybeam.isolated(fn, arg)` runs one byte entry in a fresh OS process and returns its bytes. Each call owns a supervisor with capacity 1, a 1 MiB request budget and a 3000 ms shutdown budget. Arguments and results are copied across the process boundary. Process startup adds latency and memory use. Use `TaskSupervisor` directly for configurable budgets and task handles.

Entries must be importable top-level async functions. For a script, import its entry from the script's module, as in the [home example](home.md); a function defined only in `__main__` raises `TypeError`. The child resolves the entry by module and name. Keep startup code behind `if __name__ == "__main__"` because importing the module executes its top-level statements.

The helper attempts each call once. A crash raises `pybeam.TaskCrashed` with the process exit reason; external effects remain unknown and may need reconciliation. The caller chooses any retry policy. Cancellation follows the existing task-supervisor cleanup contract; an uncooperative call may require explicit termination through a task handle.

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
