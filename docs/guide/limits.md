# Boundaries

## One local runtime

The supported bootstrap owns one BEAM VM with one main Python interpreter. The GIL still applies. Actor ownership is not multicore Python execution, a separate interpreter per actor, or protection from a C-extension crash.

The managed configuration currently rejects distribution and listener options. Existing actor references are not transparent cross-node references. Do not design around remote task or actor calls as an installed feature.

## No persistence or automatic replay

Actor state and the example ETS counters are volatile. A supervisor can replace a failed child, but does not recover its previous Python state. Applications own persistence and authorization.

A timeout or cancellation can leave an operation with effects. There is no exactly-once execution guarantee. Make retry policy explicit, especially for writes. Pub/sub is not a durable event log.

## Data and memory limits

Typed calls accept serialized data, not arbitrary Python objects. Resources belong to their owning actor and need explicit cleanup. Admission limits do not bound total process memory or arbitrary work inside a method.

The mailbox policy records in `pybeam.mailbox` describe limits but do not install enforcement. Use the bounded native `ActorGroup` API for actors. Do not infer guarantees for another API from a policy object's name.

## Keep the scope small

The public top-level API covers `actor`, `ActorGroup`, `spawn`, and `run`, with actor error types. Nodes, worker pools, Tasks, GenStage, and AI integrations are not part of this installed main API. These guides do not require them.

Start with [one actor](quickstart.md), then test the failure and shutdown behavior your application needs against the actual runtime. A successful documentation build does not verify native runtime compatibility or a clean wheel installation.
