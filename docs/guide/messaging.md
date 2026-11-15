# Shared state and messages

Store document status, then notify readers when it changes.

## Store a value

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
    await store.delete("status")


if __name__ == "__main__":
    run(main)
```

This prints `b'draft'`, then `b'published'`. Keys belong to the named store. Delete the key when it is no longer needed. Values are volatile, not persistent storage. A timeout does not undo a write.

Use an [actor](actors.md) for Python state and methods. For an atomic shared increment without an actor, use `await beam.incr("visits", 1, namespace="documents")`. Separate reads and writes are not a transaction.

## Send a message

```python
from pybeam.integrations.hornbeam.client import Beam
from pybeam.integrations.hornbeam.runtime import run


async def main() -> None:
    beam = await Beam.connect()
    async with beam.subscribe("orders", max_bytes=4096) as inbox:
        await beam.publish("orders", b"order-42")
        message = await inbox.receive()
        print(message.decode("utf-8"))


if __name__ == "__main__":
    run(main)
```

This prints `order-42`. Subscribe before publishing. Leaving the context releases the subscription. Publishing does not acknowledge that a receiver finished processing. Messages are not durable.

The Hornbeam `run` function starts and stops these services alongside Python.

## Notify readers of a changed key

```python
from pybeam.integrations.hornbeam.client import Beam
from pybeam.integrations.hornbeam.runtime import run


async def main() -> None:
    beam = await Beam.connect()
    store = beam.store("documents")
    await store.set("status", b"draft")
    async with beam.subscribe("documents:changed", max_bytes=4096) as changes:
        await store.set("status", b"published")
        await beam.publish("documents:changed", b"status")
        async for key in changes:
            print(await store.get(key.decode("utf-8")))
            break
    print(await beam.subscription_count())
    await store.delete("status")


if __name__ == "__main__":
    run(main)
```

This prints `b'published'`, then `0` subscriptions after context exit. The message contains the key. Reading that key returns its current value, which might already differ from its value when the message was published.

Breaking iteration does not release the subscription; leaving the context does. `max_bytes` bounds the queue. If it overflows, the subscription fails rather than silently dropping messages.

For a fixed stream of sample messages, see the [GenStage sample](genstage.md).
