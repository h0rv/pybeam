# Shared state

Store document status and count visits without defining an actor.

```python
from pybeam.integrations.hornbeam.client import Beam
from pybeam.integrations.hornbeam.runtime import run


async def main() -> None:
    beam = await Beam.connect()
    store = beam.store("documents")
    await store.set("status", b"draft")
    print(await store.get("status"))
    await store.set("status", b"published")
    print(await store.get("status"))
    print(await beam.incr("visits", 1, namespace="documents"))
    print(await beam.incr("visits", 2, namespace="documents"))
    await store.delete("status")


if __name__ == "__main__":
    run(main)
```

In a fresh run, this prints `b'draft'`, `b'published'`, `1`, then `3`. Keys belong to the named store. `incr` adds the amount and returns the new count as one atomic operation. Separate reads and writes are not a transaction.

Use an [actor](actors.md) when you need Python state and methods. Use `incr` for a shared counter without an actor.

Delete stored values when they are no longer needed. Values are volatile, not persistent storage. A timeout does not undo a write. The Hornbeam `run` function starts and stops these services alongside Python.

To notify readers of a change, see [Messaging](messaging.md#notify-readers-of-a-changed-key).
