# Messaging

Publish a message to every subscriber of a topic.

```python
# messaging.py
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

```console
$ uv run messaging.py
order-42
```

Subscribe before publishing; a subscriber receives only messages published after it subscribes. Leaving the `async with` block ends the subscription. `publish` does not wait for subscribers to handle the message, and messages are not stored.

## Notify readers of a changed key

```python
# subscriptions.py
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

```console
$ uv run subscriptions.py
b'published'
0
```

The topic has no subscribers after the subscription ends. The message carries only the key. Reading the key returns its current value, which may have changed again since the message was sent.

`break` stops reading but does not end the subscription; leaving the block does. `max_bytes` limits unread messages. If a subscriber falls that far behind, its subscription fails instead of silently dropping messages.
