# Distributed and durable asyncio

## Answer

asyncio's programming model can extend across a cluster, but its loop cannot. A coroutine runs to completion on the loop that started it, so nothing in this design moves suspended coroutines between nodes. A cluster can share three things instead:

1. **A cluster task group.** Start a named `async def` on any node, await its result, and cancel it, with `asyncio.TaskGroup` failure semantics. The task's state lives in memory, and a node crash fails it.
2. **A durable queue.** `put()` returns only after a replicated store has committed the item to disk on a majority of nodes. A consumer on any node leases the item and acknowledges it when done. If the consumer's node dies, the lease expires and another node retries the item. Delivery is at least once.
3. **Durable execution** (Temporal-style replay of a coroutine's steps). This is out of scope. It needs deterministic replay and versioned code, so it would turn PyBeam into a workflow engine.

The first two map well to asyncio and need no new infrastructure if they use an embedded Raft store. In same-host crash probes, Khepri, the Raft-backed store RabbitMQ uses, kept every acknowledged job and every committed state. Mnesia, which ships with OTP, kept every job but lost recently committed state changes in a full-cluster `kill -9`. Oban is the established BEAM job queue, but it requires Postgres.

None of this is implemented. PyBeam makes no durability claim today.

## What asyncio is missing

asyncio is cheap. In the [fan-in measurement](../guide/execution.md#fan-in-100000-callers), 100,000 asyncio tasks used 1.1 KB each and finished faster than BEAM callers. Its limit is scope, not speed. One loop belongs to one OS thread in one process, usually one container:

| Need | One asyncio loop | What a cluster adds |
| --- | --- | --- |
| Run more work than one process can | Run more containers, each with its own isolated loop | Any node can start, await, and cancel a task |
| Survive a process crash | Work in memory is lost | A node crash fails or retries the work instead of losing it silently |
| Keep accepted work across a crash | Needs an external broker (Redis, RabbitMQ, Postgres) | A replicated log embedded in the nodes |
| Bound how much work runs | Semaphores, per process | Admission limits enforced by the BEAM, per node and per queue |

Celery, RQ, Dramatiq and Temporal cover the last two rows with a separate broker or server. The opportunity here is the same asyncio-shaped API with the queue embedded in the runtime the application already runs.

## Current boundaries

These constraints come from the [execution model](execution-model.md), the [measured execution modes](execution-modes.md) and the source at PyBeam `57c256e` and erlang-python `7658e2f`:

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
- PyBeam: a typed `TaskGroup` over those calls, where `create_task` reads the function's `__module__` and `__name__` and rejects nested functions and lambdas.

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

`put()` returns after a quorum commit. Leaving the `async with` block without an exception acknowledges the item. An exception, a lease timeout, or the consumer's node dying returns the item to the queue with `item.attempt` incremented. A task API such as `await queue.submit(send_welcome, user_id)` can come later, built on the queue and named entries.

### Semantics the API must state

- **Acceptance.** `put()` either commits on a majority or raises. A timeout is `uncertain`: the item may exist. A `key` makes `put()` idempotent, so the caller can retry an uncertain `put()` safely.
- **At-least-once delivery.** A consumer can finish its effect and die before it acknowledges, so the item runs again. Consumers must be idempotent. No exactly-once claim.
- **Leases.** A lease has a duration and a heartbeat. A consumer whose lease expires while it is still running is fenced: its acknowledgment is rejected, and the item's next attempt has already started.
- **Retries.** Maximum attempts and backoff are queue settings. An item that runs out of attempts moves to a dead-letter state the application can inspect.
- **Availability.** A three-node cluster tolerates one node down. Without a majority, `put()` and `get()` fail rather than accept work that might be lost. A single node keeps items across a process crash, not across losing its disk.
- **Bounds.** Payload size, queue length and in-flight leases are limited, and overflow raises `Overloaded`.

## Store options

| Option | Infrastructure | Replication | Fit |
| --- | --- | --- | --- |
| [Khepri](https://github.com/rabbitmq/khepri) on [Ra](https://github.com/rabbitmq/ra) | None; embedded in each node | Raft, majority commit, fsync'd WAL | Best fit. RabbitMQ's metadata store; Apache-2.0/MPL-2.0. Lost nothing in the probes below |
| Mnesia `disc_copies` | None; ships with OTP | Synchronous replication, no consensus | Lost committed state changes in a full crash (below). Has no automatic recovery after a network partition |
| [Oban](https://github.com/oban-bg/oban) (Elixir) | Postgres | Postgres | Mature queue with retries, uniqueness, cron. Requires Postgres and an Elixir build in the runtime |
| [oban](https://pypi.org/project/oban/) 0.8.0 (Python) | Postgres | Postgres | Requires psycopg, psycopg-pool, orjson, click and uvloop (uvloop is used only by its CLI), which conflicts with a standard-library-only PyBeam. Not run on `ErlangEventLoop` |
| [procrastinate](https://pypi.org/project/procrastinate/) | Postgres | Postgres | Same dependency conflict |
| DETS or `disk_log` | None | None | Single node only; no replication |
| Horde, `pg`, `global` | None | Memory only | Placement and discovery, not durability |

Oban remains the right choice for an application that already runs Postgres and wants its queue features. PyBeam could bind Elixir Oban without any Python dependency. The default "no extra infrastructure" path needs an embedded store, and Khepri is the candidate the evidence supports.

## Crash probes

The [probe](probes/durable/) ran three Erlang nodes as separate OS processes on one host, with a separate controller. It enqueued 10,000 jobs, each counted only after the store acknowledged the write. Workers on all three nodes claimed jobs with a compare-and-swap transaction and a 2 s lease, slept 1–5 ms, and marked them done. The controller recorded every execution start outside the nodes it killed.

| Measurement | Khepri 0.19.3 / Ra 3.2.0 | Mnesia 4.27.1 `disc_copies` |
| --- | ---: | ---: |
| Acknowledged jobs lost after `kill -9` of one node (Raft leader for Khepri) | 0 | 0 |
| Jobs executed more than once in that run | 0 | 1 |
| Time from kill to a verified new leader and an acknowledged write | 223 ms | 17 ms (no leader) |
| Enqueue, one producer, sequential | 1,148 jobs/s | 2,095 jobs/s |
| Enqueue, 16 concurrent producers | 5,760 jobs/s | 8,203 jobs/s |
| Claim, run and complete, including the crash and rejoin | 256 jobs/s | 296 jobs/s |
| Killed node restarted from its data directory and rejoined | Yes | Yes |
| Jobs missing after `kill -9` of all three nodes | 0 | 0 |
| Committed states that changed after `kill -9` of all three nodes | 0 | 21 |
| Data directories after both scenarios | 69 MiB | 6.4 MiB |

In one unsynchronized full-crash run, 21 committed Mnesia states regressed (18 `done` back to `queued`, 1 `done` to `claimed`, 2 `claimed` to `queued`); a later unsynchronized run had none. One run that called `mnesia:sync_log()` on every node before the crash also had none, which is too few runs to prove the cause. The source supports a cause: `sync_transaction` waits for each node to append to `LATEST.LOG` through `disk_log`, and `disk_log` keeps up to 64 KiB or 2 s of writes in VM memory until a sync (OTP 29 `kernel/src/disk_log_1.erl:1638-1664`, `mnesia/src/mnesia_log.erl:218-259`). A committed Mnesia transaction is therefore not yet on disk. Ra's write-ahead log calls `file:datasync` on each batch before it notifies writers (Ra 3.2.0 `src/ra_log_wal.erl`, `flush_pending` and `complete_batch`).

A separate scenario killed the node holding an unfinished lease, once with the Raft leader holding it and once with a follower. In all four runs (two per store), a surviving node reclaimed the job after its 2 s lease expired and finished it on attempt 2. That second attempt is the at-least-once duplicate the API must expose.

For the full-cluster crash, the controller stopped workers, snapshotted every committed state, then killed all three nodes. This tests committed data, not transactions in flight. The failover time includes polling and RPC overhead, so it is an upper bound. These are same-host process crashes. They do not test power loss, independent hosts or network partitions, and claim throughput is bounded by the probe's 1–5 ms simulated work and its per-job transactions, not by the store.

Environment: Erlang/OTP 29.1.2 (ERTS 17.1.1), rebar3 3.28.0, Linux x86-64, 8 logical CPUs, each node `+S 2:2 +A 2`.

## Ownership and open decisions

[AGENTS.md](../../AGENTS.md) says PyBeam must not introduce a task queue or workflow engine, and must bind existing BEAM libraries instead of inventing durable queues. Khepri supplies replication and transactions, but leases, retries and dead-lettering on top of it are new queue semantics. Building them needs an explicit decision:

1. **Khepri queue in erlang-python, or a separate Erlang application.** No infrastructure, and it matches the "back-end primitives without extra infrastructure" pitch. The lease protocol would be new code and would need model checking (lease expiry, fencing, uniqueness keys) before any durability claim.
2. **Bind Elixir Oban.** Reuses a mature queue's semantics, but every deployment needs Postgres.

The recommendation is option 1, with Oban as an optional integration later. Either way, the cluster task group comes first: it needs no storage, and its named-entry transport is the same body shape a durable queue would store.

Not verified: any of these stores driven from Python through erlang-python, Khepri behavior under network partitions, multi-host latency, and payloads larger than 32 bytes.

## Reproduce

```bash
cd docs/research/probes/durable
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh store_khepri
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh store_mnesia
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh store_mnesia sync_log
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh lease store_khepri
mise exec erlang@29.1.2 rebar@3.28.0 -- bash run.sh lease store_mnesia
```

`run.sh` fetches Khepri from Hex, starts three named nodes, finds each node's OS PID with `rpc:call(Node, os, getpid, [])`, and stops nodes with `kill -9`. The default run prints one `SINGLE_CRASH` and one `FULL_CRASH` line, `sync_log` syncs Mnesia's log before the full crash, and `lease` prints `LEASE_HELD` and `LEASE_RECOVERED` lines for the leader and follower scenarios. Data directories are written under the probe directory.
