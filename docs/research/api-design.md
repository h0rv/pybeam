# A small honest Python API

## Evidence and recommendation

Keep the existing typed service boundary. If actors become necessary, prefer an explicit `CounterRef` protocol with a concrete reference implementation. Do not return an actor instance and imply that its methods are remote calls. Keep `ActorRef[T].call` as a research alternative.

This is independent design research, not MVP implementation. Existing signatures below were source inspected, not executed. All new shapes are proposals. Static results are reproduced in [probes](probes/README.md).

The checkpoint is extensions `a788a41`, Hornbeam `1217ae8`, and erlang-python `aa8f021`. I read current `AGENTS.md` and `README.md`. The archived work log and architecture explain earlier intentions, not current contracts. Runtime references were read through `git show` at these revisions. Concurrent binding and WebSocket edits are not evidence here.

## State, counters, and notifications

Hornbeam's checkpoint `priv/hornbeam/api/services.py` declares `Services`, `ByteStore`, and `Subscription`. Its `services/client.py` contains `Beam.connect`, owner binding, and these operations. Extensions has matching service protocols. Retain this small application vocabulary rather than turn every counter into an actor.

The following is a proposed use of inspected signatures, not a runtime result.

```python
from pybeam.services import Services


async def record_visit(beam: Services) -> int:
    total = await beam.incr("visits", namespace="home")
    await beam.publish("home", b"changed")
    return total
```

Use `beam.store("home").get("rendered")` for `bytes | None`. Use `async with beam.subscribe("home", max_bytes=65536)` to own notifications. Treat publication as an advisory invalidation, then read current state. The counter update and publication are separate operations, not a transaction. Do not promise durability, replication, or delivery from these signatures. Cleanup and transport limits still require runtime tests.

## Three actor candidates

The probes compare these proposed call sites.

| Candidate | Application call | Required runtime work |
| --- | --- | --- |
| Generic reference | `await ref.call(Counter.add, 2, label="visits")` | The runtime validates allowed methods and transports typed arguments and results. |
| Actor class | `await counter.add(2, label="visits")` | Distinguish an executing instance from a reference. Supply explicit forwarding methods or generated checked code. A class annotation alone supplies neither. |
| Explicit reference | `await ref.add(2, label="visits")` | Small forwarding methods call the typed runtime interface. |

`ActorRef[T].call` uses `Callable[Concatenate[T, P], Awaitable[R]]`, with `**P` as a ParamSpec. Both checkers preserve the receiver, positional arguments, keyword arguments, and awaited result in the tested signature. Both reject a bound method where an unbound method is required.

The runtime binding must own serialization and reply validation. PyBeam should call that typed interface, not add a transport or decode envelopes.

Neither checker proves that a compatible function is an actor member. The positive probe deliberately passes a free function. Likewise, structural compatibility with `CounterRef` does not prove remote identity. Explicit wrappers cost repeated declarations, but make ownership and transport visible without fabricated proxy typing.

All candidates need bounded admission, one mailbox owner, and a decision about whether another call may run while a method awaits. Remote calls additionally need versioned operations, supported value encodings, deadlines, and distinct failures for unavailable actors and malformed replies. Cancellation must distinguish abandoning a reply from stopping execution. A timed out increment may already have happened. Do not retry it automatically.

## Services, lifecycle, and inspection

A proposed background service needs only `async def run(self) -> None` initially. Bind startup, failure observation, cancellation, and bounded shutdown to existing OTP ownership. Do not equate `erlang.spawn_task` with supervision. Its checkpoint implementation schedules an asyncio task. erlang-python's `py:get_local_env` stores an environment per interpreter in the calling Erlang process. That is not an interpreter per actor. Its actor test suite was inspected, not run.

Use framework lifespan for an application handle and a request dependency for scope ownership. The checkpoint FastAPI `BeamProvider` already follows that shape. A subscription remains a nested owned resource. Application shutdown must not stop unrelated runtime services.

Propose a frozen capability record with named fields such as `counters` and `actors`, each reporting `available`, `unavailable`, or `unknown`, plus a runtime revision. Do not infer support from imports or mistake availability for authorization. No unified capability API was verified.

Before exposing supervised spawning, decide whether restart invalidates references, which pending calls fail, and whether state starts fresh. Remote cancellation, restart behavior, serialization, and actor execution were not verified. The smallest useful API remains typed service methods, explicit subscription ownership, and existing framework lifecycle adapters.
