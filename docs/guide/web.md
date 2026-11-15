# Web examples

Keep your ASGI framework and use BEAM services for shared state. The [FastAPI counter](../../examples/fastapi/main.py) uses the same counter across connections. The [Litestar](../../examples/litestar/main.py) and [PyView](../../examples/pyview/main.py) examples use the same services.

## Shared counters

Import `Beam` and `provider` from `pybeam.integrations.fastapi.dependencies`. Create the application with `FastAPI(lifespan=provider.lifespan)` and declare a route parameter as `beam: Beam`. The provider supplies services scoped to that connection.

Use `await beam.incr("shared", 1, namespace="demo")` to increment atomically. A delta of `0` reads the counter. After a change, use `await beam.publish("demo:counter", str(count).encode("ascii"))` to notify subscribers.

`provider.subscription("demo:counter")` creates a FastAPI dependency for an owned notification stream. The example reads the shared counter again after each notification rather than treating notifications as stored state. FastAPI and Litestar send updates over SSE; PyView uses LiveView WebSockets.

For cached bytes, `beam.store("session")` returns a namespaced store with async `set`, `get`, and `delete` methods. `set` accepts `expires_in` for expiring values.

## Room state

The [room miniapp](../../examples/discord_demo/main.py) uses actors for membership and messages. Join a room before sending. Crashing a room replaces its actor with empty state. Each participant must explicitly join the new generation.

Uvicorn serves FastAPI inside `ErlangEventLoop`. The Hornbeam integration owns its existing supervised services, not the HTTP listener in this example.

Disconnect releases the notification subscription, not room membership. Subscription queues and room history are bounded. Slow output disconnects after five seconds. The demo has no Discord authentication or persistent message storage.
