# PyBEAM

<!-- Logo slot: place the mark beside the heading when an asset is available. -->

PyBEAM runs Python services that keep working when one call crashes, share state and messages without Redis or a broker, and stay small in memory.

Built on [erlang-python](https://github.com/benoitc/erlang-python), [Hornbeam](https://github.com/benoitc/hornbeam) and [Erlang/OTP](https://www.erlang.org/).

```sh
uv add python-beam
```

A native segfault kills one isolated call. A second call still succeeds:

```python
# crash.py
import ctypes

import crash as entries

from pybeam import run, tasks
from pybeam.codec import Json


async def parse(payload: bytes) -> bytes:
    if Json(str).decode(payload) == "bad":
        ctypes.string_at(0)
    return payload


async def main() -> None:
    job = tasks.TaskFunction(
        entries.parse,
        Json(str),
        Json(str),
        execution=tasks.ExecutionMode.ISOLATED,
    )
    limits = tasks.TaskLimits(capacity=1, max_bytes=4096, shutdown_ms=3000)
    async with tasks.TaskSupervisor(limits) as supervisor:
        try:
            await (await supervisor.start(job, "bad"))
        except tasks.TaskCrashed as error:
            print("Call crashed:", error.reason)
        print(await (await supervisor.start(job, "Still running")))


if __name__ == "__main__":
    run(main)
```

The runtime also emits native crash diagnostics; application output:

```console
$ uv run crash.py
Call crashed: ('signal', 11)
Still running
```

Isolated tasks run in a separate OS process, so arguments and results are copied and startup costs more. See [Blocking and CPU work](execution.md).

[Get started](index.md) · [Why PyBEAM](why.md) · [Benefits and tradeoffs](tradeoffs.md)
