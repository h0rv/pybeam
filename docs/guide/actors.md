# Actors and state

## Own state through methods

`@pybeam.actor` declares dataclass state and an async interface. `await actors.spawn(Counter, value=0)` constructs the state under native ownership and returns a typed reference. Call `await counter.add(1)` rather than reading or writing `counter.value`.

References retain the class's Python field annotations, but field access raises `ActorStateAccessError` at runtime. Even child tasks cannot use actor state outside the current native dispatch task.

Constructors, method arguments, and results must fit the declared data schema. PyBEAM validates and serializes them with Pydantic. Mutable caller objects are not shared with the actor. Method failures cross this boundary as `ActorCallError` with an exception name and message, not the original exception object.

## Bound admission

`ActorGroup(max_children=128, max_pending=64, max_bytes=1_048_576)` is the default configuration. Admission checks apply before requests enter queued or running work. The request count and byte credits remain held until work ends. The byte limit concerns encoded payloads; encoded replies are size-checked too. It is not a bound on Python heap, RSS, or the cost of a method.

Do not create unlimited caller tasks merely because admission is bounded. Apply an application limit to concurrent work and handle overload explicitly. Cancelling an await does not undo a method's effects or make it safe to retry.

## Close and restart

An `async with ActorGroup()` block owns its children. Exiting revokes references and waits for native cleanup. Clean up application resources explicitly. `pybeam.spawn(...)` is the shorter context manager for one actor.

OTP can restart a permanent child with fresh state. An old reference does not silently follow the replacement. Use `await actors.rejoin(reference)` to acquire the current generation. No failed method is replayed. Restore application membership explicitly.

Use `await actors.stop(reference)` to stop an actor explicitly. Calls through stopped or closed references raise `ActorStoppedError`. For diagnostics, `await actors.info(reference)` exposes the BEAM PID and generation. `await actors.crash(reference)` kills that generation to exercise restart behavior.
