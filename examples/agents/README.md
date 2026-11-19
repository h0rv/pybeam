# Agent sessions

Run the same deterministic Pydantic AI agent on PyBEAM and on FastAPI,
Redis and Celery. The model streams text without an API key and calls tools
with measurable side effects. Each session shows its owner, transcript, tool
ledger and execution counters.

## Run both apps

The multi-node launcher uses conventional CPython 3.14 and the native runtime
built by `mise run setup`. It requires the
[development prerequisites](../../docs/development.md), a Hornbeam checkout
linked at `_checkouts/hornbeam`, and Erlang's `epmd` and `escript` on `PATH`,
like the [PyView cluster launcher](../pyview_cluster/launch.py).
Docker with the Compose plugin runs the Redis/Celery stack. Tools run in
isolated processes; free-threaded Python is not required.

From the repository root:

```sh
mise run example:agents
```

Open [PyBEAM](http://127.0.0.1:8811/) and
[Redis/Celery](http://127.0.0.1:8813/). Ctrl+C stops both stacks and removes
the conventional stack's demo data.

## Try a failure

Start a fault session and a batch of 12 **Normal** sessions. Open their links
in separate tabs to inspect the transcript and owner:

| Action | PyBEAM | Redis/Celery |
| --- | --- | --- |
| **Hang**, then **Stop session** | Terminates the isolated tool process. | Revokes the Celery task with process termination. |
| **Hang** | Occupies its own session until stopped. | Holds one of eight worker slots; later sessions queue when the slots are full. |
| **Burn** | Runs CPU work in an isolated process for about 30 seconds. | Runs CPU work inside a worker slot for about 30 seconds. |
| **Crash** | Fails that session after its tool process segfaults. | Loses a worker process and redelivers the turn; tools repeat until the session is stopped. |

Each fault follows a normal lookup. The JSON link on a session page shows
execution counts per tool-call ID. Stop hanging or repeatedly crashing sessions
when you finish inspecting them.

To kill a PyBEAM node, start a **Burn** session owned by node **a**. View its
session page on node **b** or **c**, then open node a's index page on port 8810
and use **Node failure → SIGKILL node a**. The surviving session page shows
the interrupted tool and its new owner.

To kill a Celery worker, start a **Burn** session and run the command on its
session page from the repository root:

```sh
mise exec -- uv run python -m examples.agents.usual.launch kill SESSION_ID
```

The owning worker container stops. Unacknowledged turns become eligible for
redelivery after the 60-second visibility timeout; delivery can take longer.
Turns start from the beginning and repeat tools. Restart the demo before
trying another node failure.

## Reproduce the comparison

Stop the interactive demo, then run:

```sh
mise run example:agents-compare
```

The runner starts both stacks and replaces the
[comparison data](../../benchmarks/results/agents/comparison.json) and charts.
The [agent comparison page](../../benchmarks/AGENTS_COMPARISON.md) contains the
charts, measurement method, limitations and rerun command.

## Boundaries

- PyBEAM uses three replica nodes. A completed tool result is reused on takeover.
  A started tool without a committed result is reported as interrupted and is
  **not re-run**. This is at-most-once execution, not a guarantee that an
  interrupted tool produced no external side effect.
- If you kill node **a**, restart the demo before creating more sessions.
  Existing sessions can finish on a survivor, but new sessions stay waiting:
  actor restoration reopens storage through node a, the fixed storage seed,
  even though all three nodes initialize storage at startup.
- Each PyBEAM node budgets 1 MiB of pending data for the cluster-wide full-snapshot
  subscription. The default 64 KiB is too small for concurrent token updates
  from these batches. On overflow, the node logs a warning and resubscribes;
  the next snapshot for a session resynchronizes its view. Each browser stream
  also logs and resubscribes on overflow, sending a fresh transcript without
  interrupting other streams or agent turns.
- Recovery here covers same-host SIGKILL only, not network partitions,
  whole-cluster restart or loss of the machine. PyBEAM's session directory is
  in memory; execution audit files share the host filesystem.
- Redis/Celery uses two prefork workers with four slots each and two web
  processes. Redis stores snapshots and counters; pub/sub streams snapshots
  to browsers. A task that outlives the visibility timeout can be redelivered
  even without a worker failure. Crash redelivery has no retry limit here.
- Both stacks retain only demo-sized session sets. Start a fresh run when the
  session limit is reached. Neither app is an authenticated public service.
