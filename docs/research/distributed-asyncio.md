# Distributed and durable asyncio

Historical research note: own-GIL subinterpreter mode is unsupported because common native extensions do not load in it; use free-threaded 3.14t workers or isolated OS processes instead. See the [compatibility results](execution-modes.md#unsupported-own-gil-subinterpreters).

## Answer

asyncio's programming model can extend across a cluster, but its loop cannot. A coroutine runs to completion on the loop that started it, so nothing in this design moves suspended coroutines between nodes. A cluster can share three things instead:

1. **A cluster task group.** Start a named `async def` on any node, await its result, and cancel it, with `asyncio.TaskGroup` failure semantics. The task's state lives in memory, and a node crash fails it.
2. **A durable queue.** `put()` returns only after the store has synced the item to disk. A consumer leases the item and acknowledges it when done. If the consumer's node dies, the lease expires and another consumer retries the item. Delivery is at least once.
3. **Durable execution** (Temporal-style replay of a coroutine's steps). This is out of scope. It needs deterministic replay and versioned code, so it would turn PyBEAM into a workflow engine.

Recommend built-in Mnesia as the default store, starting with one node. Log syncing stays on by default: a per-node group-commit process batches writers, calls `mnesia:sync_log()`, then replies. A write returns only after every active replica has synced. Both synced variants retained acknowledged jobs and states in the crash probes below; unsynced Mnesia regressed states.

Keep one Python API, with the storage backend hidden behind it. Khepri/Ra is a later backend for deployments that need consensus and partition safety on three or more nodes. Oban remains an option for applications that already use Postgres.

None of this is implemented. PyBEAM makes no durability claim today.

## What asyncio is missing

asyncio is cheap. In the [fan-in measurement](../guide/internals/measurements.md#fan-in-many-beam-processes-8-contexts), 100,000 asyncio tasks used 1.1 KB each and finished faster than BEAM callers. Its limit is scope, not speed. One loop belongs to one OS thread in one process, usually one container:

| Need | One asyncio loop | What a cluster adds |
| --- | --- | --- |
| Run more work than one process can | Run more containers, each with its own isolated loop | Any node can start, await, and cancel a task |
| Survive a process crash | Work in memory is lost | A node crash fails or retries the work instead of losing it silently |
| Keep accepted work across a crash | Needs persistent storage or an external broker | A disk-backed store embedded in the runtime |
| Bound how much work runs | Semaphores, per process | Admission limits enforced by the BEAM, per node and per queue |

Celery, RQ, Dramatiq and Temporal cover the last two rows with a separate broker or server. The opportunity here is the same asyncio-shaped API with the queue embedded in the runtime the application already runs.

## Current boundaries

These constraints come from the [execution model](execution-model.md), the [measured execution modes](execution-modes.md) and the source at PyBEAM `57c256e` and erlang-python `7658e2f`:

- `ErlangEventLoop` is a local asyncio loop. Its tasks do not become OTP processes and cannot migrate.
- `TaskSupervisor` starts each task as a temporary OTP child of a local `py_task_group` (`src/py_task_group.erl`). Failure does not cancel siblings, and nothing restarts.
- Worker-mode task bodies are interpreter-local closures retained by reference (`erlang._actor_retain`). A closure cannot cross a node or survive a restart. `py_task_group` already accepts **named entries** (`{start_named, Mode, ...}`) checked as a Python module and function name. A named entry plus a bytes payload is the only body shape that can be sent to another node or stored.
- Cross-node calls in `src/py_distribution.erl` use OTP `erpc` for one fixed demonstration operation (`double`) with an 8 KiB limit. When a node is unreachable, the call reports `uncertain` and never replays.
- Hornbeam state and counters are node-local ETS tables. Pub/sub uses OTP `pg` across nodes. Nothing is persisted.

## Cluster task group

### Proposed API

This extends `asyncio.TaskGroup` to remote placement. The body is a top-level `async def`, which the runtime resolves by module and name on the target node, as with owngil entries. It never pickles the function.

```python
# jobs.py
async def thumbnail(image: bytes) -> bytes:
    return image[:1024]
```

```python
# main.py
from pybeam.tasks import TaskGroup

from jobs import thumbnail


async def main(images: list[bytes]) -> list[bytes]:
    async with TaskGroup(placement="cluster") as group:
        tasks = [group.create_task(thumbnail, image) for image in images]
    return [task.result() for task in tasks]
```

### Semantics

- **Placement.** The runtime chooses a node with free admission capacity, or the caller names one. A task starts once, on one node, and stays there.
- **Failure.** The first failure cancels the remaining tasks and raises an `ExceptionGroup`, as in `asyncio.TaskGroup`. Losing a node raises `NodeLost` for its tasks. Their side effects may already have happened, and the runtime does not retry them.
- **Cancellation.** Cancelling the group sends an OTP exit to every remote task process, and each node cancels its coroutine on its own loop. Cancellation cannot undo completed effects or interrupt a blocked C call.
- **Data.** Arguments and results cross nodes as bytes produced by codecs on the caller's side. Live objects, sockets and locks never cross.

### What this needs

- erlang-python: start a named entry through `py_task_group` on a remote node through `erpc`, and monitor the remote task across nodes. OTP monitors already report `noconnection` when a node goes down.
- erlang-python: named entries in worker mode. They are accepted only for owngil today.
- PyBEAM: a typed `TaskGroup` over those calls, where `create_task` reads the function's `__module__` and `__name__` and rejects nested functions and lambdas.

OTP `pg` groups and `erpc` cover placement and transport, so a distributed registry such as Horde or Swarm is not needed.

## Durable queue

### Proposed API

The queue mirrors `asyncio.Queue`. The difference is that `get()` returns a lease, which the consumer must acknowledge.

```python
# producer.py
from pybeam.queues import DurableQueue


async def signup(user_id: int) -> None:
    emails = DurableQueue("emails")
    await emails.put(str(user_id).encode(), key=f"welcome:{user_id}")
```

```python
# consumer.py
from pybeam.queues import DurableQueue


async def send_welcome_emails() -> None:
    emails = DurableQueue("emails")
    while True:
        async with emails.get() as item:
            await send_welcome(int(item.payload))
```

With the proposed default store, `put()` returns after the transaction and a log sync on every active replica (one replica on a single node). Leaving the `async with` block without an exception acknowledges the item. An exception, a lease timeout, or the consumer's node dying returns the item to the queue with `item.attempt` incremented. A task API such as `await queue.submit(send_welcome, user_id)` can come later, built on the queue and named entries.

### Semantics the API must state

- **Acceptance.** The default Mnesia store returns success only after the transaction and log sync on every active replica. A timeout is `uncertain`: the item may exist. A `key` would make retries of an uncertain `put()` idempotent.
- **At-least-once delivery.** A consumer can finish its effect and die before it acknowledges, so the item runs again. Consumers must be idempotent. No exactly-once claim.
- **Leases.** A lease has a duration and a heartbeat. A consumer whose lease expires while it is still running is fenced: its acknowledgment is rejected, and the item's next attempt has already started.
- **Retries.** Maximum attempts and backoff are queue settings. An item that runs out of attempts moves to a dead-letter state the application can inspect.
- **Availability.** Start with one node: items survive a process crash, not loss of its disk. Symmetric three-node Mnesia survived one node down in the probe, but it does not provide partition protection. A later Khepri backend would require a majority for writes and consistent reads. Hiding the backend must not hide these failure boundaries.
- **Bounds.** Payload size, queue length and in-flight leases are limited, and overflow raises `Overloaded`.

## Store options

| Option | Infrastructure | Replication | Fit |
| --- | --- | --- | --- |
| Mnesia `disc_copies`, with group-commit log sync | None; ships with OTP | Synchronous replication, no consensus | Recommended default, single-node first. Retained acknowledged states in the tested single and symmetric non-majority configurations |
| [Khepri](https://github.com/rabbitmq/khepri) on [Ra](https://github.com/rabbitmq/ra) | None; embedded in each node | Raft, majority commit, fsync'd WAL | Later backend for 3+ node consensus and partition safety. RabbitMQ's metadata store; Apache-2.0/MPL-2.0. Lost nothing in these probes |
| [Oban](https://github.com/oban-bg/oban) (Elixir) | Postgres | Postgres | Mature queue with retries, uniqueness, cron. Requires Postgres and an Elixir build in the runtime |
| [oban](https://pypi.org/project/oban/) 0.8.0 (Python) | Postgres | Postgres | Requires psycopg, psycopg-pool, orjson, click and uvloop (uvloop is used only by its CLI), which conflicts with a standard-library-only PyBEAM. Not run on `ErlangEventLoop` |
| [procrastinate](https://pypi.org/project/procrastinate/) | Postgres | Postgres | Same dependency conflict |
| DETS or `disk_log` | None | None | Single node only; no replication |
| Horde, `pg`, `global` | None | Memory only | Placement and discovery, not durability |

Oban remains a good choice for an application that already runs Postgres and wants its queue features. Binding Elixir Oban would avoid its Python client's dependencies, but still needs Postgres and an Elixir build. Mnesia provides the default embedded-store path without either dependency.

## Mnesia commit paths

`sync_transaction` is not an fsync. In OTP 29.1.2, Mnesia's synchronous log append uses `disk_log:log/blog` ([`mnesia_log.erl:223–259`](https://github.com/erlang/otp/blob/OTP-29.1.2/lib/mnesia/src/mnesia_log.erl#L223-L259)). `sync_log()` takes a separate path through `disk_log:sync`, which flushes the cache and calls `file:sync` ([`mnesia_monitor.erl:395–396`](https://github.com/erlang/otp/blob/OTP-29.1.2/lib/mnesia/src/mnesia_monitor.erl#L395-L396), [`disk_log_1.erl:1707–1715`](https://github.com/erlang/otp/blob/OTP-29.1.2/lib/kernel/src/disk_log_1.erl#L1707-L1715)).

The transaction protocol matters:

- **Majority-enabled tables: syncing the log is insufficient.** Majority metadata selects `asym_trans` ([`mnesia_tm.erl:1379–1387`](https://github.com/erlang/otp/blob/OTP-29.1.2/lib/mnesia/src/mnesia_tm.erl#L1379-L1387)). The coordinator queues its committed decision through `mnesia_recover`; that process logs the decision asynchronously ([`mnesia_tm.erl:1692–1704`](https://github.com/erlang/otp/blob/OTP-29.1.2/lib/mnesia/src/mnesia_tm.erl#L1692-L1704), [`mnesia_recover.erl:180–181`](https://github.com/erlang/otp/blob/OTP-29.1.2/lib/mnesia/src/mnesia_recover.erl#L180-L181), [`252–270`](https://github.com/erlang/otp/blob/OTP-29.1.2/lib/mnesia/src/mnesia_recover.erl#L252-L270)). A forced-window witness suspended that process, received write success and `sync_log() = ok` on all three replicas, then killed all nodes. The acknowledged job was missing after restart on every replica.
- **Single-node and symmetric non-majority tables: the tested commit path supports a later log sync.** `sync_transaction` selects `sync_sym_trans`, with `presume_commit` records ([`mnesia_tm.erl:1174–1243`](https://github.com/erlang/otp/blob/OTP-29.1.2/lib/mnesia/src/mnesia_tm.erl#L1174-L1243)). The coordinator synchronously appends its record and waits for participant completion; participants also append before replying ([`1512–1542`](https://github.com/erlang/otp/blob/OTP-29.1.2/lib/mnesia/src/mnesia_tm.erl#L1512-L1542), [`302–324`](https://github.com/erlang/otp/blob/OTP-29.1.2/lib/mnesia/src/mnesia_tm.erl#L302-L324)). Recovery treats `presume_commit` as committed without a separate decision log ([`mnesia_recover.erl:440–442`](https://github.com/erlang/otp/blob/OTP-29.1.2/lib/mnesia/src/mnesia_recover.erl#L440-L442)). Both configurations survived the same forced-window witness with the recovery process suspended.

These findings cover jobs-only transactions with matching `disc_copies` placement, no majority flag, and no schema changes. They do not cover arbitrary multi-table or asymmetric transactions. The durable probe variants sync every active replica, not just the coordinator. They use documented Mnesia APIs only; suspending the recovery process is test instrumentation, not part of the store.

## Crash probes

The [probe sources](probes/durable/) run one or three Erlang OS processes on one host, plus a separate controller. Each workload enqueues 10,000 acknowledged jobs. Twelve workers claim jobs with compare-and-swap transactions and 2 s leases, sleep 1–5 ms, then mark them done. The controller records acknowledged states and execution starts outside the nodes it kills.

The matrix kills nodes while workers are still claiming and completing jobs. It does not stop workers or read a pre-crash store snapshot. Recovery may be newer than the controller's last acknowledgement; an older state or a conflicting state at the same version counts as a regression. This replaces the earlier stopped-worker snapshot comparison.

### Full-crash results

Each Mnesia row lists five trials in order. Khepri ran one full-crash trial. Counts are per workload, not summed across replicas. All replicas recovered identical snapshots.

| Store | Replicas | Lost acknowledged jobs | State regressions | Jobs executed more than once |
| --- | ---: | --- | --- | --- |
| Mnesia unsynced | 1 | 0 in all trials | 62, 66, 78, 117, 126 | 62, 66, 75, 117, 122 |
| Mnesia sync-each | 1 | 0 in all trials | 0 in all trials | 1, 11, 9, 12, 7 |
| Mnesia group-commit | 1 | 0 in all trials | 0 in all trials | 4, 7, 0, 3, 9 |
| Mnesia unsynced | 3 | 0 in all trials | 106, 35, 160, 110, 6 | 106, 35, 160, 109, 9 |
| Mnesia sync-each | 3 | 0 in all trials | 0 in all trials | 4, 3, 1, 2, 1 |
| Mnesia group-commit | 3 | 0 in all trials | 0 in all trials | 6, 2, 10, 9, 4 |
| Khepri | 3 | 0 | 0 | 8 |

Every three-node store also survived a single-node crash with zero lost jobs or state regressions, and the killed node rejoined. Duplicate-execution job counts were 4 for unsynced Mnesia, 4 for sync-each, 2 for group-commit, and 1 for Khepri. Khepri's actual leader was killed. With one replica, the full-crash test is also the single-node crash test.

A separate held-lease scenario killed a follower and then a leader, once per store. In all four runs, a survivor reclaimed the expired lease and finished on attempt 2. Mnesia has no leader; those labels select the second and first nodes.

### Throughput

Jobs/s, rounded. Sequential and concurrent enqueue each use 10,000 fresh IDs; concurrency is 16. Claim/complete includes simulated work, controller RPCs, and completion checks. The final column also includes the single-node crash and rejoin.

| Store | Replicas | Enqueue sequential | Enqueue concurrent | Claim/complete | Claim/complete with crash |
| --- | ---: | ---: | ---: | ---: | ---: |
| Mnesia unsynced | 1 | 14,144 | 38,168 | 886 | — |
| Mnesia sync-each | 1 | 2,206 | 1,585 | 594 | — |
| Mnesia group-commit | 1 | 288 | 4,013 | 634 | — |
| Mnesia unsynced | 3 | 3,537 | 14,993 | 2,093 | 1,244 |
| Mnesia sync-each | 3 | 423 | 2,144 | 540 | 630 |
| Mnesia group-commit | 3 | 187 | 3,353 | 728 | 501 |
| Khepri | 3 | 932 | 9,718 | 950 | 445 |

The 1 ms group-commit window slows sequential writes. Under concurrent enqueue it delivered 2.53× sync-each throughput on one replica and 1.56× on three. Batches reached 16 callers. Each three-node batcher handled 40,000 durability requests in about 13,800 `sync_log()` calls. Physical fsync syscall counts were not traced; `disk_log` may also combine concurrent sync requests internally.

### Boundaries and failures

- These are same-host process crashes, not power-loss, disk-loss, independent-host, or partition tests. Symmetric three-node Mnesia has no partition protection. Do not treat its results as consensus guarantees.
- Log syncing does not prevent duplicate execution after an interrupted claim. Consumers still need idempotent effects.
- Worker transient exceptions are retried, not individually logged. An initial matrix launch failed because peer code paths were relative; the copied launcher uses absolute build paths. Khepri restart logged discarded replies to old node incarnations; its recovered snapshots still had zero loss or regressions.
- The matrix does not verify Python integration or payloads larger than 32 bytes.

Environment: Erlang/OTP 29.1.2 (ERTS 17.1.1), Mnesia 4.27.1, Khepri 0.19.3, Ra 3.2.0, rebar3 3.28.0, Linux x86-64, 8 logical CPUs, each node `+S 2:2 +A 2`.

## Ownership and open decisions

[AGENTS.md](../../AGENTS.md) keeps queue and workflow engines outside PyBEAM's ownership. Mnesia supplies the proposed default storage, not leases, retry policy, fencing, or dead-letter handling. The prototype must settle ownership of those semantics and test lease expiry, uniqueness, and uncertain outcomes before making a product durability claim.

Recommend one Python API over a single-node Mnesia store with batched syncing enabled by default. Keep backend selection in deployment configuration. Add Khepri/Ra later for three-or-more-node consensus and partition safety, and leave Oban as an optional integration for Postgres users. None of these queue APIs or store bindings is implemented in PyBEAM today.

## Reproduce

Run the forced-window witnesses first. `majority` should recover a missing job; `single` and `symmetric` should recover the job.

```bash
cd docs/research/probes/durable
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh witness majority
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh witness single
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh witness symmetric
```

Run the matrix with `matrix STORE REPLICAS FULL_CRASH_TRIALS`. All Mnesia matrix runs disable the majority flag.

```bash
for replicas in 1 3; do
  for store in store_mnesia store_mnesia_sync_each store_mnesia_synced; do
    mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh matrix "$store" "$replicas" 5
  done
done
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh matrix store_khepri 3 1
```

`run.sh` changes to its own directory and fetches Khepri from Hex. The matrix starts named OS processes, obtains their PIDs with `rpc:call(Node, os, getpid, [])`, and uses `kill -9`. It prints `ENQUEUE`, `WORK`, `ACTIVE`, `CRASH`, `RECOVERY`, and `MATRIX_DONE` lines. Data directories and binary comparison snapshots stay in the ignored probe directory paths; do not commit them.

The earlier stopped-worker and held-lease probes remain available:

```bash
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh store_mnesia sync_log
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh lease store_khepri
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh lease store_mnesia
```

The legacy `store_mnesia sync_log` run enables majority and syncs before a snapshot. A zero-mismatch legacy run does not override the forced-window failure above.
