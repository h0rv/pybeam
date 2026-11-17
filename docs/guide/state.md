# Shared state

Store values and count events across requests, without defining an actor.

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

In a fresh run, this prints `b'draft'`, `b'published'`, `1`, then `3`. Each store keeps its own keys. `incr` adds to a counter and returns the new value in one atomic operation, so concurrent requests never lose an increment. A separate `get` and `set` is not atomic.

Start programs that use shared state with `run` from `pybeam.integrations.hornbeam.runtime`.

Values live in memory and are lost when the program stops. Delete keys you no longer need. For state with its own methods, use an [actor](actors.md).

To tell readers that a value changed, see [Messaging](messaging.md#notify-readers-of-a-changed-key).
