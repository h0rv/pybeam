# Actor capacity: plain sync code versus async

How many actors fit in one PyBeam process, what each costs, and what happens when an actor method runs blocking code. All numbers are measured; nothing here is a production limit.

## Setup

- Linux x86-64 VM, 8 CPUs, 31 GiB memory, kernel 6.8, `ulimit -u` 128356.
- CPython 3.14.10 (conventional, GIL enabled), Erlang/OTP 29.1.2.
- erlang-python `d60392c`, as locked in `rebar.lock`; default `ActorGroup` (worker contexts, shared loop).
- Two runs of [`pybeam_actors.py`](probes/capacity/pybeam_actors.py); one run of [`plain_python.py`](probes/capacity/plain_python.py). Where the runs differ, both values are shown.
- Network calls go to [`server.py`](probes/capacity/server.py) on localhost, which answers each `GET` after 50 ms.
- Free-threaded 3.14t, own-GIL contexts, and `ExecutionLimits` actor groups were not measured.

## How an actor method runs

Each spawned actor owns one erlang-python worker context. In the [native actor](https://github.com/h0rv/erlang-python/blob/d60392cdd86a5947f0569d83e0f8dfefcec83410/src/py_data_actor.erl) the context is `py_context:new(#{mode => worker})`, and worker contexts [share the main interpreter's event loop](https://github.com/h0rv/erlang-python/blob/d60392cdd86a5947f0569d83e0f8dfefcec83410/src/py_context.erl#L638-L639). The probe confirms it: `threading.get_ident()` inside every actor method equals the caller's thread.

So every actor method body runs on one event loop thread. An `await` lets other actors run. A blocking call inside an `async def` method stops every actor in the process until it returns.

## Cost per actor

| Count | RSS | OS threads | Live contexts | Time to spawn this step |
| --- | --- | --- | --- | --- |
| 0 | 84.9 MiB | 19 | 1 | |
| 100 | 91.3 MiB | 119 | 101 | 0.27 s |
| 1,000 | 142.8 MiB | 1,019 | 1,001 | 2.3 s |
| 5,000 | 375.2 MiB | 5,019 | 5,001 | 6.9 s |
| 10,000 | 669.1 MiB | 10,019 | 10,001 | 12.5–14.8 s |

Each idle actor added about **60 KiB RSS and one OS thread**. Spawning took 2.3–3.0 ms per actor and slowed as the count grew; reaching 10,000 took about 24 s. 10,000 was the last checkpoint; spawning did not fail. RSS reached about 920 MiB after the call tests below.

For comparison, plain Python on the same machine:

| 10,000 of | RSS added | OS threads | Start time |
| --- | --- | --- | --- |
| PyBeam actors | ~584 MiB | 10,000 | ~24 s |
| `threading.Thread` blocked on an `Event` | 206 MiB | 10,000 | 1.2 s |
| `asyncio` tasks blocked on an `Event` | 8.5 MiB | 0 | 0.03 s |

## Calls

All rows call one method on N distinct actors at once with `asyncio.gather`, unless noted.

| Method body | N | Wall time |
| --- | --- | --- |
| `await asyncio.sleep(0.05)` | 1 / 10 / 100 | 0.052 / 0.054 / 0.071–0.086 s |
| `await asyncio.sleep(0.05)` | 1,000 / 4,000 / 8,000 | 0.33–0.51 / 1.6–2.1 / 2.8–4.3 s |
| `await asyncio.sleep(0.05)` | 10,000 | Failed: `RuntimeError: Native callback queue full` |
| `time.sleep(0.05)` | 1 / 8 / 32 | 0.053 / 0.405 / 1.62 s |
| `await asyncio.to_thread(time.sleep, 0.05)` | 8 / 32 / 100 | 0.054 / 0.16 / 0.46 s |
| `await asyncio.sleep(0.01)` beside one `time.sleep(0.3)` | 2 | 0.311 s |
| Empty method, 1,000 sequential calls on one actor | 1 | 0.41–0.82 ms per call |

- **Blocking code serializes all actors.** 32 actors each sleeping 50 ms took 32 × 50 ms. A 10 ms async call waited behind another actor's 300 ms `time.sleep`.
- **Async methods overlap**, but per-call overhead dominates past about 100 concurrent calls: 8,000 calls took 2.8–4.3 s for 50 ms of waiting.
- **About 8,000 calls can be in flight from one caller loop.** erlang-python caps reserved native callbacks per loop at 8,192 (`MAX_RESERVED_PENDING` in `c_src/py_event_loop.h`). The overflow raised in the caller; after the error, closing the group also hit the full queue and the managed shutdown deadline expired.
- **`asyncio.to_thread` restores overlap** for blocking code, up to the default executor's 12 threads on 8 CPUs: 100 calls took 9 rounds of 50 ms.
- The ~40 ms acknowledgment delay under separate investigation did not appear: one empty call took under 1 ms.

## Fanning out blocking network calls

One actor method makes 10 blocking `http.client` requests to the 50 ms server.

| Method body | Wall time |
| --- | --- |
| 10 requests in a loop | 0.53–0.57 s |
| 10 requests through a `ThreadPoolExecutor(10)` inside the method | 0.055 s |
| 10 requests through `asyncio.gather(asyncio.to_thread(...))` | 0.055 s |
| 10 actors, each fanning out 10 requests with `to_thread` | 0.47 s |

Fan-out cuts one actor's wall time from 10 × 50 ms to one round trip. The `ThreadPoolExecutor` version still blocks the shared loop, and every other actor, for those 55 ms. With `to_thread`, 100 concurrent requests are limited by the 12-thread default executor.

## Tasks for comparison

`TaskFunction` bodies doing `time.sleep(0.05)`, all started at once:

| Loop | N = 8 | N = 64 |
| --- | --- | --- |
| Shared (default) | 0.43 s | 3.37 s |
| `dedicated_loop=True` | 1.08 s | 1.19 s |

Shared-loop tasks serialize like actors. Dedicated-loop tasks overlap blocking calls but include the ~1 s untraced completion overhead described in [execution modes](execution-modes.md). Actors have no dedicated-loop option.

## What this measures and what it does not

On this build, an actor is not a cheap sequential process. It costs a native thread and about 60 KiB, and plain blocking code in one actor stalls the rest. Sync code gets concurrency only by moving blocking calls to threads.

Not measured:

- Free-threaded 3.14t actors. The method body still runs on the one shared loop thread, so the serialization above is expected to remain, but this was not run.
- Own-GIL or isolated actors. Actors are worker-only in this erlang-python revision.
- `ActorGroup(execution=ExecutionLimits(...))`. The locked erlang-python `DataActorGroup` accepts only `max_children`.
- Memory or thread limits beyond 10,000 actors, CPU-bound methods, or container cgroup limits below this VM's.

## Reproduce

Build the native runtime and point a project at it as described in [development](../development.md#use-a-development-runtime). Then, from the repository root:

```sh
mise exec -- python docs/research/probes/capacity/server.py 38999 &
mise exec -- python docs/research/probes/capacity/plain_python.py threads 10000
mise exec -- python docs/research/probes/capacity/plain_python.py tasks 10000
PROBE_PORT=38999 mise exec -- uv run --locked python docs/research/probes/capacity/pybeam_actors.py
```

Run the last command from a directory whose `pyproject.toml` sets `development_runtime`.

The PyBeam probe prints one row per spawn checkpoint and one `RESULT` JSON line, and takes about a minute.
