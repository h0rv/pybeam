# PyBEAM

Python services that keep working when one call crashes, share state and messages in the VM, and stay small in memory.

```sh
uv add python-beam
```

Built on [erlang-python](https://github.com/benoitc/erlang-python), [Hornbeam](https://github.com/benoitc/hornbeam) and [Erlang/OTP](https://www.erlang.org/).

This call segfaults in a child process. The caller catches the failure and keeps running:

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

The runtime also prints native crash diagnostics. The static import gives the child an importable entry name. Each [isolated call](execution.md#isolated-calls) starts a separate OS process and copies its bytes; startup costs more, execution runs once, and effects after a crash remain unknown.

## Measured results

- **One crash stays in one isolated call:** [0 of 20 clients dropped](tradeoffs.md#one-failure-stays-in-one-call), versus every client on the crashed Uvicorn worker.
- **Counters and pub/sub shared in the VM:** [400/400 increments and 8/8 subscribers](tradeoffs.md#shared-state-in-the-vm), versus Uvicorn's 188–286/400 and 1–5/8.
- **Less idle memory:** [165 MiB versus 2.3 GiB](tradeoffs.md#cost-to-run) for the FastAPI+Celery port with 64 task slots.
- **Resume after a node is killed:** [0.13 s versus 144 s](tradeoffs.md#recovery-after-a-node-dies) for Redis/Celery in one recorded run per stack.

The cost: [about 12–38% fewer requests per second](tradeoffs.md#what-pybeam-costs) than tuned Uvicorn/Granian in the HTTP probe. These small samples indicate direction; capacity depends on the workload. State is in memory and node-local.

[Get started](index.md) · [Why PyBEAM](why.md) · [Benefits and tradeoffs](tradeoffs.md)
