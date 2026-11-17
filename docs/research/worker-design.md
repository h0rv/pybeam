# Worker design: actors in the BEAM, Python on a worker pool

A design proposal measured against [actor capacity](actor-capacity.md). Nothing here is implemented. The numbers come from that page (8 CPUs, CPython 3.14.10 and 3.14.10t, erlang-python `d60392c`).

## Problem

Today a PyBEAM actor is a native worker context with its own OS thread, and every actor method body runs on one shared asyncio loop thread. On both 3.14 and 3.14t:

- each actor costs one thread and 50–60 KiB;
- `time.sleep` or CPU work in one actor stalls every actor (32 × 50 ms took 1.62–1.65 s);
- about 8,000 calls can be in flight from one caller loop.

The same worker contexts called directly from BEAM processes run Python on all 8 CPUs on 3.14t. A BEAM process costs 2.7 KB. The bottleneck is the shared loop in the actor path, not the GIL or the context.

## Design

1. **The actor is a BEAM process.** Its name, mailbox, supervision, placement, and durable state copy live in the BEAM. Actor count is bounded by BEAM memory (2.7 KB per process), not by Python threads.
2. **Python runs on a pool of identical workers.** Each worker is a 3.14t worker context with its own thread. All workers load the same application code, so any worker can run any actor. Pool size starts at the CPU count and grows by one worker whenever a message finds none free, up to a cap. In the pool probe, growing from 8 to 33 workers cut the wait behind 32 blocked 200 ms calls from 781 ms to 21.6 ms, and a worker costs 1.1 ms to create and under 0.1 MiB. On a FastAPI and Pydantic request path, 8 3.14t threads served 10,760 requests/s, against at most 2,905 on 3.14. Scaling out means more workers per node and more nodes.
3. **One message runs at a time per actor, on one worker.** The actor process takes the next message from its mailbox, sends it to a worker, and waits for the reply. Plain `def` methods are the default; `async def` methods also work for existing asyncio code. A blocking call holds only its own worker.
4. **Python state stays pinned to the worker that last ran the actor.** The BEAM routes the actor's next message back to that worker, so no state moves on the common path. Pinned state costs one call (27 µs on 3.14, 42–45 µs on 3.14t) at any size. When the pinned worker is busy, the message waits for it up to a timeout, then any free worker loads the durable copy. This is Temporal's sticky queue rule; its Go SDK waits 5 s by default (`stickyWorkflowTaskScheduleToStartTimeoutSeconds`), and its Python SDK waits 10 s (`sticky_queue_schedule_to_start_timeout`).
5. **State lives in memory by default; the actor writes its durable copy explicitly.** The BEAM stores each written copy with a version. A worker loads the copy only when the actor moves or its worker dies. This follows Orleans, where modified grain state "isn't automatically persisted" until the grain calls `WriteStateAsync`, and `gen_server`, whose state lives only in process memory. Without a write, a worker crash loses the actor's state since its last write. Each copy carries a version, so a stale worker cache is detected, not silently used. Moving state costs mostly encoding: 0.6 ms at 3,300 entries and 7–9 ms at 33,000 through ETS, against 1.4–1.7 ms and 17 ms for JSON over HTTP or Redis. Pass large data by reference, such as a URI or key, rather than inside the state. The durable copy uses `pickle`: it handles dataclasses, which `marshal` and `json` reject. Locks, sockets, lambdas, and generators cannot be encoded by any of the three, so they stay in pinned state and are rebuilt after a move.
6. **Crash-prone code opts into isolated workers.** An isolated worker is a child OS process. It is the only mode that survives `os.abort()`, at 21–27 MiB and 61–132 ms to start per process. The same actor API selects the pool kind; application code does not change.
7. **Own-GIL is removed.** 14 of 17 common compiled packages, including pydantic-core, uvloop, numpy, and SQLAlchemy, refuse to load in subinterpreters. `ExecutionMode.OWNGIL` and its named-entry path go away, and the chat demo's `CHECKSUM` job moves from `ExecutionMode.OWNGIL` to a 3.14t worker.

8. **Module globals are shared.** On 3.14t, all workers run in one interpreter, so module-level mutable state is shared across actors and threads. CPython's free-threading guide leaves synchronization to the program and recommends `threading.Lock` over relying on built-in types' internal locks. PyBEAM documents this and points users to actor state or the shared store.

One API covers all of it: an actor class, its methods, a state write call, and the pool kind. Mode choices are deployment settings, not separate programming models.

## Reuse before building

- Actor processes, mailboxes, and supervision: OTP processes and supervisors, as erlang-python's actor and task paths already use.
- Worker contexts and isolated children: erlang-python `py_context` `worker` and `isolated` modes.
- Durable copy: ETS first, then Mnesia when state must survive a node restart.
- Routing a message to its pinned worker: a BEAM-side map from actor to worker, owned by the pool. No Python scheduler.
- Later, across nodes: OTP `global` for cluster-wide names, `pg` for process groups, and Ra (Raft) for agreed ownership.

## Open questions

- **Call-rate ceiling.** Worker-context calls reached about 32,000–43,000 per second on both builds, with 8 contexts and 1M callers. Own-GIL reached 116,000–185,000 per second through the same path. The cause is being traced in the scheduler work, and the pool design depends on the result.
- **Retiring workers.** Growth was measured; retirement was not. An idle worker costs under 0.1 MiB, so an idle timeout matters less than the cap.
- **3.14t single-thread cost.** On the FastAPI path, one 3.14t thread was 34% slower than one 3.14 thread, so 3.14t wins only with two or more busy workers.
- **The sticky timeout value.** Temporal uses 5 s (Go) and 10 s (Python). PyBEAM's value needs a measurement of state-load cost against queueing.

Out of scope for now: GPU workers and per-queue worker pools.
