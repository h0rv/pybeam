# Web and integrations

Keep your ASGI framework. Use BEAM services from HTTP handlers, background services, or live connections. Hornbeam supplies ASGI serving and existing supervised services rather than a new Python web framework.

## Lifespans and HTTP handlers

Import `Beam` and `provider` from `pybeam.integrations.fastapi.dependencies`. Create the application with `FastAPI(lifespan=provider.lifespan)` and declare a route parameter as `beam: Beam`. The provider supplies service access scoped to the connection.

The [FastAPI](../../examples/fastapi/main.py), [Litestar](../../examples/litestar/main.py), and [PyView](../../examples/pyview/main.py) examples use Hornbeam's ASGI serving path. In the [room miniapp](../../examples/discord_demo/main.py), Uvicorn instead serves FastAPI inside `ErlangEventLoop`. That example uses Hornbeam's supervised services without its HTTP listener.

## Counters, storage, and notifications

`await beam.incr("shared", 1, namespace="demo")` increments atomically. A delta of `0` reads the counter. `beam.store("session")` provides async `set`, `get`, and `delete` for namespaced bytes. `set` accepts `expires_in` for expiring values.

`await beam.publish(topic, payload)` publishes bytes. `provider.subscription(topic)` creates a FastAPI dependency for an owned notification stream. The counter example reads shared data again after notifications. FastAPI and Litestar send updates over SSE; PyView uses LiveView WebSockets.

Litestar's `BeamChannelsBackend` adapts subscriptions to its channels interface. Subscription and framework merge queues have separate bounds. Disconnect cleanup belongs to the connection's owner, not to a garbage collector.

## GenStage streams

For demand-driven processing, `pybeam.integrations.genstage.pipeline.stream` owns a finite GenStage graph in an async context manager. Supply a count and an importable Python function that transforms bytes to bytes. Consume results with async iteration.

The [GenStage example](../../examples/genstage.py) uses a typed callback to parse requests and return results. The graph admits one request at a time, including running callback work. Context exit waits for graph cleanup. This is a bounded processing primitive, not persistent job storage or a workflow engine.
