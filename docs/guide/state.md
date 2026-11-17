# Shared state

Store values and count events across requests, without defining an actor.

```python
# state.py
from pybeam.hornbeam import Beam, run


async def main() -> None:
    beam = await Beam.connect()
    store = beam.store("documents")

    await store.set("status", b"draft")
    print(await store.get("status"))

    await store.set("status", b"published")
    print(await store.get("status"))

    visits = await beam.incr("visits", 1, namespace="documents")
    print(visits)

    visits = await beam.incr("visits", 2, namespace="documents")
    print(visits)

    await store.delete("status")


if __name__ == "__main__":
    run(main)
```

```console
$ uv run state.py
b'draft'
b'published'
1
3
```

Each store keeps its own keys. `incr` adds to a counter and returns the new value in one atomic operation, so concurrent requests never lose an increment. A separate `get` and `set` is not atomic.

Start programs that use shared state with `run` from `pybeam.hornbeam`.

Values live in memory and are lost when the program stops. Delete keys you no longer need. For state with its own methods, use an [actor](actors.md).

To tell readers that a value changed, see [Messaging](messaging.md#notify-readers-of-a-changed-key).
