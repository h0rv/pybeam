# Startup, reconnects, and failures

Treat a restart as a new execution and a WebSocket reconnect as a new connection.
PyBEAM can restart work, but it does not make every value or message durable.
Choose what to save, and rebuild each client's view from authorized current state.

## Initialize before serving requests

In an ASGI application's lifespan startup, you can connect to shared services,
initialize state, and create and call actors before yielding to serve requests.
Keep resources inside their owning lifespan or actor group.

If lifespan startup raises an exception, startup stops and the original error
appears in the startup error output. Requests are not served by that failed
startup. Fix the error before retrying; do not treat a process starting as proof
that your application is ready.

See [Shared state](state.md), [Actors](actors.md), and your
[FastAPI](integrations/fastapi.md) or [Litestar](integrations/litestar.md)
integration for resource setup.

## Rebuild a view after WebSocket reconnect

A reconnect does not migrate the previous connection or replay missed messages.
Your connection handler runs again. Authenticate and authorize the new connection
even if the previous one was accepted.

When your handler rejects a WebSocket with close code `1008`, the client completes
the handshake and then receives that close code, rather than an HTTP rejection.
Do not send a view before authorization succeeds.

For a view driven by shared state and change notifications:

1. Authenticate the connection and authorize the data it may access.
2. Subscribe to changes for that authorized data.
3. Read current shared state and render a fresh authorized view.
4. On each notification, read the state again rather than treating the message as
   the source of truth.
5. End the subscription when the connection ends.

Subscribing before the initial read avoids a gap between reading state and
starting to receive notifications. A notification may refer to a change already
included in your initial read; reading current state again is safe.

Messages published while the client has no subscription are missed. Reconnecting
does not recover them. If your application needs a history, store it separately
and authorize reads of that history. See [Messaging](messaging.md).

## Know what survives a failure

| Failure or boundary | Lost or stopped | What survives or recovers |
| --- | --- | --- |
| Actor crash followed by a supervised restart | The actor's unsaved in-memory changes. An ordinary actor starts again from its constructor state. | Shared counters and store values on the still-running node. An actor restored from explicitly saved state recovers its saved values, not later unsaved changes. |
| Runtime stop or node loss | That node's in-memory shared counters and store values, and its live connections and subscriptions. | Explicitly saved actor state survives a whole-runtime process kill when its saved files remain available. Durable task records can recover as described below. |
| WebSocket disconnect | The connection and its subscription; messages published during the gap are not replayed. | Shared state on the running node. A new authorized connection can read it to build a fresh view. |
| Client falls behind its subscription's unread-message limit | The subscription fails and is released instead of retaining an unbounded backlog. A nonreading WebSocket can also be disconnected. | Shared state remains available for a fresh read after reconnecting. |
| Orderly shutdown with plain background tasks still running | The tasks are cancelled; cooperative cleanup can run within the shutdown budget. | Cleanup effects that completed. Cancellation-resistant work cannot report a clean shutdown. |
| Durable task worker-node loss | The current execution does not continue from its suspended position. | A serving replica can rerun the recorded task from the beginning. Acknowledged submissions also recover after a runtime process crash when their stored data remains available. |

Do not treat ordinary background tasks as recorded jobs. The shutdown behavior
above is cancellation, not durable recovery. An abrupt process kill cannot run
cooperative cleanup.

Saved actor state and durable tasks depend on keeping their data files. Neither
supervision nor shared in-memory state replaces persistent storage. Recovery from
a process crash does not establish recovery from disk loss or power failure.

Durable tasks can repeat external effects: design task bodies for retries. Read
the [durable task storage and replication limits](durable-tasks.md) before relying
on recovery. See [Supervision](supervision.md) for replacement actor references
and [Actors](actors.md) for call timeout behavior.
