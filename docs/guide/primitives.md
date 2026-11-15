# Core primitives

Start with an async Python entry function. Add the primitives your application needs. Functions describe work, classes describe state, and owned scopes define how long either may live.

## Background jobs and tasks

A finite task represents one computation with a result. Typed submission pairs an async function with request and result codecs. Its handle lets the caller observe completion or request cancellation. An owned task supervisor retains responsibility for the body and its cleanup, even if a caller stops waiting. A supervised task is not detached fire-and-forget work, and its failure need not cancel sibling tasks.

For a long-running async service, `pybeam.services.background` exposes `ServiceSlot` and `ServiceRef`. `ServiceSlot(capacity=1)` admits one live service. Start an importable module-level async function with `slot.start(function, ...)`, then use `await ref.wait()` to observe completion or `await ref.stop()` to request cancellation and await cleanup. A wait timeout does not stop the service. Arguments on this local service API are passed directly, not serialized.

## Channels

A channel separates a producer from a consumer. Python sends and receives typed values; async iteration consumes a stream. The underlying queue holds serialized data rather than shared mutable objects.

Queue capacity and encoded-byte limits apply to queued messages. Dequeue releases those credits before the consumer finishes processing. Closing the send side allows queued data to drain. Aborting discards it. Keep ownership of the channel until producers and consumers have stopped.

## Message passing

Use a request and result when one caller needs an answer. Use publish/subscribe when several receivers need a notification. Neither form transfers a live Python object to another owner.

For Hornbeam services, `await beam.publish(topic, payload)` publishes bytes. `async with beam.subscribe(topic) as messages` owns a subscription, and `async for payload in messages` receives notifications. A publish result is not proof that a subscriber completed its work.

## Actors

Use `@pybeam.actor` for state with typed async methods. `await actors.spawn(Counter, value=0)` returns a reference. Call `await counter.add(1)` rather than accessing `counter.value`. Calls are serialized, including while a method awaits. Arguments and results are validated copies. Method failures become `ActorCallError`, not the original exception object.

`ActorGroup` bounds admitted queued plus running requests and their encoded bytes. Credits remain held until work ends. This differs from channel queue limits. Neither limit bounds RSS or arbitrary work inside a method.

## Supervision and lifetimes

`async with ActorGroup()` owns its actor children. Exit revokes references and waits for cleanup. `await actors.stop(reference)` stops one child. Calls through stopped references raise `ActorStoppedError`.

OTP restart policies govern replacement, not replay. A restarted actor has fresh state. `await actors.rejoin(reference)` explicitly acquires its new generation. Restore application membership separately.

For application-owned OTP trees, `SupervisorRef` exposes child inspection and explicit stop, restart, and deletion operations. Its logical child IDs differ from the process generations reported by actor diagnostics. Supervision owns processes; application code still owns persistence and resource cleanup.

## Web composition

An ASGI lifespan can own service connections while request handlers borrow scoped access. A background service can publish notifications independently of a request. Framework adapters translate those notifications into SSE or WebSocket updates. See [web and integrations](web.md) for concrete APIs.
