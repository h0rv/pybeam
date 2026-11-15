# Boundaries

## Execution

Python runs in one BEAM VM with one main interpreter. The GIL still applies. Separate actor processes do not provide multicore Python execution or isolation from a C-extension crash.

An actor serializes its calls even while a method awaits. Use separate actors for independent state owners. Use ordinary async functions when you only need concurrent I/O and no actor state.

## Effects and persistence

Actor state and ETS counters are volatile. OTP can replace a failed child, but does not recover its previous Python state. Applications own persistence and authorization.

A timeout or cancellation is not rollback. The method may already have changed state. There is no exactly-once execution guarantee or automatic replay. Make retry policy explicit, especially for writes. Pub/sub is not a durable event log.

## Data and memory

Actor calls transfer serialized request and result copies, not arbitrary Python objects. Resources belong to their actor and require explicit cleanup.

Admission limits count accepted work and encoded request bytes. They do not bound total process memory, serialization scratch space, or the work a method can perform. Bound the number of caller tasks as well as the actor's admitted work.
