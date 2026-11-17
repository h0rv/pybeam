# Durable tasks

Durable tasks record work before running it. A worker starts the function again
after a crash; it does not resume a suspended coroutine. This is an at-least-once
prototype, not production-ready.

## Record work and run workers

Put the body in an importable module. It must be a plain top-level async function
that accepts bytes and returns bytes. Every worker needs the same application code.

```python
# durable_body.py
import asyncio
from pathlib import Path


async def write_report(payload: bytes) -> bytes:
    await asyncio.to_thread(Path("report.txt").write_bytes, payload)
    return b""
```

The entry point records a report and owns two workers until you stop it.
Writing the same file contents again is safe for this example; design your own
external effects to tolerate retries too.

```python
# durable_tasks.py
import asyncio

from durable_body import write_report

import pybeam
from pybeam.queues import DurableTasks


async def main() -> None:
    tasks = DurableTasks("reports")
    async with tasks.serve(concurrency=2, lease=60.0):
        await tasks.submit(write_report, b"Report ready\n", key="report:42")
        print("Task recorded; workers running.", flush=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    pybeam.run(main)
```

```console
$ uv run durable_tasks.py
Task recorded; workers running.
```

Press Ctrl-C to stop. Results are discarded; successful execution acknowledges
the task. Exiting `serve` stops this node's workers and releases their claims when
possible. Otherwise, their leases expire.

## Storage and retries

- `submit` returns `True` after the store syncs the write, or `False` for a retained
  duplicate key. Cancellation is uncertain: the task may already be stored.
- Keys last only until acknowledgement. A submit key prevents duplicate queued
  records, not duplicate external effects. Bodies must be idempotent or use
  application idempotency keys.
- Keep the data directory across restarts. It defaults to `.pybeam/data`; configure
  `data_dir` under `[tool.pybeam]` to change it. Do not share one directory between
  running nodes.
- `max_items` includes queued and leased tasks. Full queues raise `QueueFull`.
  `PayloadTooLarge` means the stored module/function/payload record exceeds 1 MiB;
  storage and runtime failures raise `QueueStoreError`.
- Body errors release the task for retry. There is no dead-letter queue or attempt
  cap yet. Leases are not renewed: a body running longer than its lease can overlap
  another execution, even without a crash.

## Replicas and limits

Empty `replicas` means this node only. For an already connected, trusted cluster,
pass the same sequence of node names to every `DurableTasks` instance. Each node
must open the same queue with the same capacity, use its own persistent directory,
and enter `serve` to run workers there. Replica startup must overlap while the
store joins. This API does not provision a cluster; the local distribution demo
is limited to two nodes.

After node loss or lease expiry, another serving replica can claim the task and
rerun its body from the start. Accepted writes sync on every active write replica,
not necessarily every configured node when one is down. Replicated Mnesia uses
no majority rule and is **not partition-safe**. Do not treat it as consensus or
exactly-once execution.

Recovery tests cover same-host process crashes, including three-node reruns. They
do not establish power-loss durability, multi-host reliability, partition safety,
or production-load limits.
