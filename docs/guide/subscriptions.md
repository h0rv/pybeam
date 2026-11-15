# Subscriptions

Receive an invalidation, then read the authoritative value.

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

This prints `b'published'`, then `0` subscriptions after context exit. The message contains a key, not a second copy of the stored value.

Breaking iteration does not itself release the subscription; the enclosing context does. Queues are bounded. Handle `SubscriptionOverflow` when a consumer cannot keep up. See [web](web.md) for connection-scoped use.
