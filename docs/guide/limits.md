# Boundaries

## Execution and ownership

Python runs in one BEAM VM with one main interpreter. The GIL still applies. OTP processes do not provide multicore Python execution or isolation from a C-extension crash.

Keep ownership of background work until its body and cleanup have ended. Cancelling a waiter is not the same as stopping its work. Cancellation is cooperative; a timeout is not proof of cleanup.

## Effects and delivery

A timeout or cancellation is not rollback. Work may already have changed state or performed an external write. There is no exactly-once execution guarantee. Choose retry policy explicitly.

OTP can replace a failed process, but replacement does not restore previous Python state or replay missed messages. Actor state and ETS values are volatile. Pub/sub is not a durable event log. Applications own persistence and authorization.

## Data and bounds

Serialized messages are data copies, not remote Python objects. Actor arguments and results cross this boundary. The local background service API instead passes arguments directly within its worker loop.

Channel credits cover queued messages and return on dequeue. Actor admission credits cover queued plus running requests and remain held until work ends. Neither is a bound on RSS, temporary serialization allocations, or arbitrary work in application code. Bound caller concurrency as well.
