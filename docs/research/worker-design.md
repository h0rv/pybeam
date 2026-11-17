# Worker design: actors in the BEAM, Python on a worker pool

A design proposal measured against [actor capacity](actor-capacity.md). Nothing here is implemented. The numbers come from that page (8 CPUs, CPython 3.14.10 and 3.14.10t, erlang-python `d60392c`).

## Problem

Today a PyBeam actor is a native worker context with its own OS thread, and every actor method body runs on one shared asyncio loop thread. On both 3.14 and 3.14t:

- each actor costs one thread and 50–60 KiB;
- `time.sleep` or CPU work in one actor stalls every actor (32 × 50 ms took 1.62–1.65 s);
- about 8,000 calls can be in flight from one caller loop.

The same worker contexts called directly from BEAM processes run Python on all 8 CPUs on 3.14t. A BEAM process costs 2.7 KB. The bottleneck is the shared loop in the actor path, not the GIL or the context.

## Design

1. **The actor is a BEAM process.** Its name, mailbox, supervision, placement, and durable state copy live in the BEAM. Actor count is bounded by BEAM memory (2.7 KB per process), not by Python threads.
2. **Python runs on a pool of identical workers.** Each worker is a 3.14t worker context with its own thread. All workers load the same application code, so any worker can run any actor. Pool size defaults to the CPU count, plus headroom for blocked calls. Scaling out means more workers per node and more nodes.
3. **One message runs at a time per actor, on one worker.** The actor process takes the next message from its mailbox, sends it to a worker, and waits for the reply. Plain `def` methods are the default; `async def` methods also work for existing asyncio code. A blocking call holds only its own worker.
4. **Python state stays pinned to the worker that last ran the actor.** The BEAM routes the actor's next message back to that worker, so no state moves on the common path. Pinned state costs one call (27 µs on 3.14, 42–45 µs on 3.14t) at any size.
5. **The BEAM keeps a versioned durable copy, per a write-back setting.** `every_message`, `periodic`, or `never`. A worker loads the copy only when the actor moves or its worker dies. Each copy carries a version, so a stale worker cache is detected, not silently used. Moving state costs mostly encoding: 0.6 ms at 3,300 entries and 7–9 ms at 33,000 through ETS, against 1.4–1.7 ms and 17 ms for JSON over HTTP or Redis. Pass large data by reference, such as a URI or key, rather than inside the state.
6. **Crash-prone code opts into isolated workers.** An isolated worker is a child OS process. It is the only mode that survives `os.abort()`, at 21–27 MiB and 61–132 ms to start per process. The same actor API selects the pool kind; application code does not change.
7. **Own-GIL is removed.** 14 of 17 common compiled packages, including pydantic-core, uvloop, numpy, and SQLAlchemy, refuse to load in subinterpreters. `ExecutionMode.OWNGIL` and its named-entry path go away, and the chat demo's `CHECKSUM` job moves from `ExecutionMode.OWNGIL` to a 3.14t worker.

One API covers all of it: an actor class, its methods, and two settings (pool kind and write-back). Mode choices are deployment settings, not separate programming models.

## Reuse before building

- Actor processes, mailboxes, and supervision: OTP processes and supervisors, as erlang-python's actor and task paths already use.
- Worker contexts and isolated children: erlang-python `py_context` `worker` and `isolated` modes.
- Durable copy: ETS first, then Mnesia when state must survive a node restart.
- Routing a message to its pinned worker: a BEAM-side map from actor to worker, owned by the pool. No Python scheduler.

## Open questions

- **Call-rate ceiling.** Worker-context calls reached about 32,000–43,000 per second on both builds, with 8 contexts and 1M callers. Own-GIL reached 116,000–185,000 per second through the same path. The cause is being traced in the scheduler work; the pool design depends on the result.
- **Pinning and fairness.** What happens when the pinned worker is busy: wait, or move the actor and pay the state load?
- **Pool growth for blocked calls.** How many extra workers, and when to retire them.
- **Write-back default.** `every_message` costs encoding on every message; `never` loses state on a worker crash.
- **State codec.** Pickle was measured. Other codecs and the rules for what state may hold (open sockets, locks) are unmeasured.
- **Module globals.** Workers share one interpreter on 3.14t, so module-level mutable state is shared across actors. The library cannot prevent it, only document it.
- **3.14t on a web request path.** Only an integer loop was compared (same speed on both builds).
- **Callback queue limit.** The 8,192 in-flight cap per caller loop applies to the current async call path; whether the pool path inherits it is untested.

Out of scope for now: GPU workers and per-queue worker pools.
