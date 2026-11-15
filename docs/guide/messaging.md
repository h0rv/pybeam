# Messaging

Send bytes to a topic and receive them through an owned subscription.

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

This prints `order-42`. Subscribe before publishing so the receiver is ready. Leaving the context releases the subscription.

The Hornbeam `run` function starts and stops its services alongside Python. A publish does not acknowledge that a receiver finished processing. Use [subscriptions](subscriptions.md) when a message means shared data has changed.
