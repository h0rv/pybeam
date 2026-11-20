# Crash containment

A native extension can segfault while a Python function runs. These examples call `ctypes.string_at(0)` to trigger that failure. Run them as separate scripts: the first deliberately kills its Python process.

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

The runtime also prints native crash diagnostics. Each [isolated call](../execution.md#isolated-calls) starts a separate OS process and copies its byte arguments and result. Startup adds latency and memory use. The helper attempts each call once; external effects after a crash remain unknown and may need reconciliation.

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
