# Why PyBEAM

Asyncio lets your Python app wait on many things at once. It does not decide who owns shared state, how readers learn it changed, or what replaces a failed actor. PyBEAM brings BEAM/OTP tools to those jobs through typed Python APIs.

Keep your async functions and [web framework](web.md). Use [actors](actors.md) for state with serial method calls, [shared counters](state.md) for atomic increments, and [pub/sub](messaging.md) to notify readers. You choose the ownership and failure boundaries; PyBEAM handles the runtime lifecycle.

## Python at the surface, OTP underneath

![Architecture: Python app, PyBEAM typed wrappers, erlang-python and optional Hornbeam services, then BEAM/OTP supervisors, ETS shared state, pub/sub and mailboxes.](assets/architecture.svg)

PyBEAM is a thin Python-facing layer, not a new scheduler or web framework. erlang-python owns execution, contexts and message passing. Hornbeam supplies the current counter, store and pub/sub services; these services do not require replacing your ASGI server. OTP supplies supervision underneath the actor API.

Ordinary asyncio tasks remain asyncio tasks, not OTP processes. Today's build runs Python in one main interpreter with its GIL, so Python code does not run on multiple cores yet. In the current build, OTP processes do not isolate C-extension crashes.

## Shared state, explicit recovery

Imagine two WebSocket clients connected to your async app. Both need the same count, not a separate Python variable per connection. This is a composition of [shared state](state.md), [change notifications](messaging.md#notify-readers-of-a-changed-key) and your framework's connection handling, not a new browser protocol.

![Two clients share one atomic counter through Python handlers. A changed-key notification prompts fresh reads. Separately, an OTP supervisor replaces a crashed actor with fresh state.](assets/shared-state.svg)

1. A handler calls `incr`: the increment and returned count are one atomic operation.
2. After the update, the app publishes a changed-key notification. This is a separate operation, not a transaction with the write.
3. Subscribers read the current count and send it to their clients. An invalidation is a prompt to read, not a historical snapshot or delivery guarantee.
4. Separately, if a supervised actor crashes, OTP constructs a replacement. The app uses `rejoin` to acquire its new reference. The old state is lost, the old reference does not follow it, and no method is replayed.

The shared counter does not need an actor. Actor restart does not restore the counter or provide persistence. Stored values are volatile; pub/sub messages are not durable. Leaving a subscription context releases it. A full subscription queue fails explicitly rather than silently dropping messages.

## Plain asyncio vs PyBEAM

| Need | Plain asyncio | With PyBEAM |
| --- | --- | --- |
| Concurrent I/O | Tasks and coroutines | Keep tasks and coroutines inside the runtime |
| Own mutable state | Arrange access and locking in your app | Actor methods execute serially, even across awaits |
| Share a counter | Choose and coordinate a state owner or service | Hornbeam provides an atomic BEAM counter |
| Notify readers | Choose queues or a messaging service | Pub/sub with explicit subscription lifetime and byte limits |
| Replace a failed actor | Arrange a restart policy | OTP restarts from the constructor; explicitly acquire the replacement |

PyBEAM is useful when you want these BEAM ownership and messaging semantics in a Python app. If tasks and local state already meet your needs, plain asyncio may be enough. Neither choice makes your data durable automatically. Timeouts stop waiting; they do not undo effects, and retries can repeat them.

## Try one boundary

This [messaging example](messaging.md) subscribes before publishing and releases the subscription on context exit:

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

It prints `order-42`. Publishing does not acknowledge that a receiver finished processing. Next, try [shared state](state.md) or [actor supervision](supervision.md).
