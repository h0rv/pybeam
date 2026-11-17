# Why PyBEAM

When concurrent requests share data, an async Python app usually needs locks, a Redis server for counters and pub/sub, and restart logic you write yourself. PyBEAM provides these primitives in your Python program, without extra infrastructure.

## No locks

- An [actor](actors.md) owns its state and runs one method call at a time, even while a method awaits. Calls never interleave inside an actor, so its state needs no lock.
- Arguments and results are copied, so callers never share a mutable object with an actor.
- A [shared counter](state.md) changes and returns its value in one atomic step.
- [Messaging](messaging.md) notifies subscribers without a separate broker.
- [Supervision](supervision.md) replaces a failed actor with a fresh one.

## Example: two clients, one count

Two WebSocket clients connect to your app and need to see the same count.

![Two clients share one atomic counter through Python handlers. A changed-key notification prompts fresh reads. Separately, a supervisor replaces a crashed actor with fresh state.](assets/shared-state.svg)

1. A handler calls `incr`. The increment and the returned count are one atomic operation.
2. The handler publishes a message naming the changed key.
3. Each subscriber reads the current count and sends it to its client.
4. If a supervised actor crashes, the supervisor starts a replacement from its constructor. Call `rejoin` to get the new reference.

## Compared with plain asyncio

| Need | Plain asyncio | PyBEAM |
| --- | --- | --- |
| Mutable state shared by tasks | Locks around every access | Actor methods run one at a time |
| Shared counter | Redis or a lock | Atomic `incr` |
| Notify other connections | Redis pub/sub or a broker | `publish` and `subscribe` |
| Recover from a failure | Your own restart code | Supervisor restarts the actor |

If tasks and local variables already meet your needs, plain asyncio is enough.

## Limits

- State and messages live in memory and do not survive a restart. Keep durable data in your database.
- A timeout stops waiting; it does not undo the call. Retrying can repeat its effects.
- A restarted actor starts from its constructor. The old state and any in-flight calls are lost.

To learn how calls use threads and CPU cores, see [How Python work runs](execution.md).

## Try it

Subscribe to a topic, publish a message, and receive it:

```python
# messaging.py
from pybeam.integrations.hornbeam import Beam, run


async def main() -> None:
    beam = await Beam.connect()

    async with beam.subscribe("orders", max_bytes=4096) as inbox:
        await beam.publish("orders", b"order-42")

        message = await inbox.receive()
        print(message.decode("utf-8"))


if __name__ == "__main__":
    run(main)
```

```console
$ uv run messaging.py
order-42
```

Next, try [shared state](state.md) or [supervision](supervision.md).
