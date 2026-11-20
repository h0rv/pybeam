# Survive a segfault

A native extension can segfault while a Python function runs. These examples call `ctypes.string_at(0)` to trigger that failure. Run them as separate scripts: the first deliberately kills its Python process.

## How it works

`SIGSEGV` arrives while native code runs, before the interpreter regains control. The operating system ends the process; `try/except` sees Python exceptions. A Python `signal.signal(SIGSEGV, ...)` handler waits for the interpreter to resume. Returning to the faulting C instruction can trigger the signal repeatedly and hang the process, as [CPython's signal documentation](https://docs.python.org/3/library/signal.html#execution-of-python-signal-handlers) explains. Recovery belongs across a process boundary.

Plain Python offers `multiprocessing` and `ProcessPoolExecutor` for that boundary. [Process pools pickle their callables, arguments and results](https://docs.python.org/3/library/concurrent.futures.html#processpoolexecutor). A dead worker breaks the pool, as the queued and later calls below demonstrate.

Each `pybeam.isolated` call gets its own OS child process, started by erlang-python and watched by the BEAM. A child exit with signal 11 becomes `TaskCrashed(('signal', 11))`. The next call starts a fresh child. Requests and results cross the boundary as copied bytes; the helper attempts each call once. External effects after a crash remain unknown and may need reconciliation.

Ten sequential calls returning their child PID take **141.0 ms median per call** on the local development runtime, including supervisor setup, child startup, the call and cleanup. All ten return distinct child PIDs. This is one sample on a shared Linux VM using conventional CPython 3.14.10 and OTP 28.3. Rerun the [timing probe](../../../examples/docs/isolated_cost.py) from the repository root with the [development runtime configured](../../development.md#use-a-development-runtime):

```sh
uv run --locked python examples/docs/isolated_cost.py
```

## Plain asyncio

`try/except` handles Python exceptions. A segmentation fault ends the process before the handler or the following calculation can run.

```python
# crash_asyncio.py
import asyncio
import ctypes


async def crash(payload: bytes) -> bytes:
    ctypes.string_at(0)
    return payload


async def main() -> None:
    print("Calling native code", flush=True)
    try:
        await crash(b"")
    except Exception as error:
        print("Call raised:", type(error).__name__)
    print("Still running:", 6 * 7)


if __name__ == "__main__":
    asyncio.run(main())
```

```console
$ uv run crash_asyncio.py
Calling native code
$ echo $?
139
```

On Linux, shell status 139 reports signal 11 (`SIGSEGV`). Shells may also print a segmentation-fault diagnostic.

## An isolated PyBEAM call

The child process dies and the caller receives `TaskCrashed`. The caller then performs its calculation. The static import gives the child an importable module and entry name; keep startup code behind the `__main__` guard.

```python
# crash.py
import ctypes

import crash as entries

import pybeam


async def crash(payload: bytes) -> bytes:
    ctypes.string_at(0)
    return payload


async def main() -> None:
    try:
        await pybeam.isolated(entries.crash, b"")
    except pybeam.TaskCrashed as error:
        print("Call crashed:", error.reason)
    print("Still running:", 6 * 7)


if __name__ == "__main__":
    pybeam.run(main)
```

```console
$ uv run crash.py
Call crashed: ('signal', 11)
Still running: 42
```

The runtime also prints native crash diagnostics. [Execution budgets and cancellation](../execution.md#isolated-calls) apply to each call.

## A process pool

`ProcessPoolExecutor` also keeps the caller alive. In this single-worker example, a worker's death raises `BrokenProcessPool` for the crashing call, the queued call and a later submission. The pool needs replacement before it can accept work again. The short sleep lets the caller queue another call before the crash.

```python
# crash_pool.py
import ctypes
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool


def crash() -> bytes:
    time.sleep(0.1)
    ctypes.string_at(0)
    return b""


def main() -> None:
    with ProcessPoolExecutor(max_workers=1) as pool:
        failed = pool.submit(crash)
        pending = pool.submit(bytes, b"queued")
        calls = (("Crashing call", failed), ("Pending call", pending))
        for label, future in calls:
            try:
                future.result()
            except BrokenProcessPool:
                print(label + ": BrokenProcessPool")
        try:
            pool.submit(bytes, b"later")
        except BrokenProcessPool:
            print("Later call: BrokenProcessPool")
    print("Caller still running")


if __name__ == "__main__":
    main()
```

```console
$ uv run crash_pool.py
Crashing call: BrokenProcessPool
Pending call: BrokenProcessPool
Later call: BrokenProcessPool
Caller still running
```

These outputs use conventional CPython 3.14 on Linux. See [Benefits and tradeoffs](../tradeoffs.md) for the service-level probes and their reproduction commands.
