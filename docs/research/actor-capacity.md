# Actor capacity: plain sync code versus async

How many actors fit in one PyBeam process, what each costs, what happens when an actor method runs blocking code, and how each Python execution mode behaves under the same load. All numbers are measured; nothing here is a production limit.

## Setup

- Linux x86-64 VM, 8 CPUs, 31 GiB memory, kernel 6.8, `ulimit -u` 128356.
- CPython 3.14.10 conventional (GIL enabled) and 3.14.10t free-threaded, Erlang/OTP 29.1.2. Each build used its own `py_nif.so`, checked with `ldd`.
- erlang-python `d60392c`, as locked in `rebar.lock`; default `ActorGroup` (worker contexts, shared loop).
- [`pybeam_actors.py`](probes/capacity/pybeam_actors.py): three runs on 3.14, one on 3.14t. [`plain_python.py`](probes/capacity/plain_python.py): one run on 3.14. [`modes_matrix.py`](probes/capacity/modes_matrix.py): one run on 3.14, two on 3.14t. Where runs differ, both values are shown.
- Network calls go to [`server.py`](probes/capacity/server.py) on localhost, which answers each `GET` after 50 ms.
- `ExecutionLimits` actor groups were not measured.

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

On 3.14t, 10,000 actors added 494 MiB (about 50 KiB each) and the same 10,000 threads.

Each idle actor added about **60 KiB RSS and one OS thread** on 3.14. Spawning took 2.3–3.0 ms per actor and slowed as the count grew; reaching 10,000 took about 24 s. 10,000 was the last checkpoint; spawning did not fail. RSS reached about 920 MiB after the call tests below.

For comparison, plain Python on the same machine:

| 10,000 of | RSS added | OS threads | Start time |
| --- | --- | --- | --- |
| PyBeam actors | ~584 MiB | 10,000 | ~24 s |
| `threading.Thread` blocked on an `Event` | 206 MiB | 10,000 | 1.2 s |
| `asyncio` tasks blocked on an `Event` | 8.5 MiB | 0 | 0.03 s |

## Calls

All rows call one method on N distinct actors at once with `asyncio.gather`, unless noted. The CPU job is a 2,000,000-step integer loop that takes about 0.12 s alone.

| Method body | N | 3.14 | 3.14t |
| --- | --- | --- | --- |
| `await asyncio.sleep(0.05)` | 1 / 10 / 100 | 0.051–0.052 / 0.054 / 0.071–0.086 s | 0.051 / 0.053 / 0.10 s |
| `await asyncio.sleep(0.05)` | 1,000 / 4,000 / 8,000 | 0.33–0.51 / 1.6–2.1 / 2.8–4.3 s | 0.52 / 3.5 / 12.8 s |
| `await asyncio.sleep(0.05)` | 10,000 | Failed: `RuntimeError: Native callback queue full` | Not run |
| `time.sleep(0.05)` | 1 / 8 / 32 | 0.053 / 0.41 / 1.62 s | 0.062 / 0.41 / 1.65 s |
| `await asyncio.to_thread(time.sleep, 0.05)` | 8 / 32 / 100 | 0.054 / 0.16 / 0.46 s | 0.065 / 0.17 / 0.49 s |
| CPU job in the method | 1 / 8 | 0.12 / 0.96 s | 0.14 / 1.06 s |
| `await asyncio.to_thread(cpu_job)` | 1 / 8 | 0.12 / 1.0 s | 0.16 / 0.17 s |
| `await asyncio.sleep(0.01)` beside one `time.sleep(0.3)` | 2 | 0.311 s | 0.317 s |
| Empty method, 1,000 sequential calls on one actor | 1 | 0.41–0.82 ms per call | 0.61 ms per call |

- **Blocking code serializes all actors on both builds.** 32 actors each sleeping 50 ms took 32 × 50 ms. A 10 ms async call waited behind another actor's 300 ms `time.sleep`. Removing the GIL does not change this, because every method body runs on the one loop thread.
- **CPU work in a method body serializes on both builds.** Eight 0.12 s jobs took about 1 s. On 3.14t, the same jobs through `to_thread` took 0.17 s; on 3.14 they did not overlap.
- **Async methods overlap**, but per-call overhead dominates past about 100 concurrent calls: 8,000 calls took 2.8–4.3 s on 3.14 and 12.8 s on 3.14t for 50 ms of waiting.
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

| Loop | N = 8, 3.14 / 3.14t | N = 64, 3.14 / 3.14t |
| --- | --- | --- |
| Shared (default) | 0.43 / 0.51 s | 3.35–3.37 / 3.99 s |
| `dedicated_loop=True` | 1.08 / 1.18 s | 1.18–1.19 / 2.12 s |

Shared-loop tasks serialize like actors. Dedicated-loop tasks overlap blocking calls but include the ~1 s untraced completion overhead described in [execution modes](execution-modes.md). Actors have no dedicated-loop option.

## Python contexts called directly from BEAM processes

[`modes_matrix.py`](probes/capacity/modes_matrix.py) drives the modes lab's [`probe.escript`](../../examples/modes_lab/probe.escript). Each of N Erlang processes calls its own erlang-python context with `py_context:call`, with no asyncio loop in between. The CPU job is a 3,000,000-step integer loop that takes about 0.33 s alone. The blocking job is `time.sleep(0.05)` from [`blocking/job.py`](probes/capacity/blocking/job.py).

| Mode | CPU job, N = 1 / 8 / 64, 3.14 | CPU job, N = 1 / 8 / 64, 3.14t | `time.sleep(0.05)`, N = 64 |
| --- | --- | --- | --- |
| Worker | 0.33 / 2.86 / 22.9 s | 0.33–0.34 / 0.33–0.43 / 2.68–2.77 s | 0.053 s / 0.054–0.135 s |
| Own-GIL | 0.34 / 0.38 / 2.86 s | 0.33–0.34 / 0.34–0.66 / 2.69–2.98 s | 0.051 s / 0.052–0.055 s |
| Isolated | 0.25 / 0.35 / 2.31 s | 0.27–0.28 / 0.32–0.64 / 2.31–2.70 s | 0.053 s / 0.053–0.054 s |

| Mode | Create one context | RSS per context, 3.14 / 3.14t | Empty call p50, 3.14 / 3.14t | `os.abort()` in one of 4 contexts |
| --- | --- | --- | --- | --- |
| Worker | 1.1–1.2 ms | under 0.1 MiB / under 0.1 MiB | 28–36 / 41–42 µs | VM exits |
| Own-GIL | 75–83 ms | 10.8–11.3 / 15.7–16.2 MiB | 12–15 µs | VM exits |
| Isolated | 61–132 ms | 21.3–22.3 / 25.7–26.8 MiB | 52–401 µs | Other 3 survive; replacement in 64–134 ms |

- **On 3.14t, worker contexts run Python on all 8 CPUs.** Eight CPU jobs took as long as one, and 64 took 8 rounds. On 3.14 they serialize.
- **The actor slowdown comes from the shared loop, not the GIL.** The same worker contexts that parallelize here serialize when PyBeam actors dispatch through one asyncio loop.
- **This integer loop ran at the same speed on 3.14 and 3.14t** (0.33–0.34 s alone). That says nothing about a web request path, which was not measured.
- **Blocking calls overlap in every mode on both builds**, because each context has its own thread.

## Many BEAM processes sharing 8 contexts

The `fanin` probe spawns N Erlang processes. Each one calls an empty Python function on one of 8 contexts, with at most 32 calls in flight.

| Processes | Memory per process | Spawn time | Worker, 3.14 / 3.14t | Own-GIL, 3.14 / 3.14t | Isolated, 3.14 / 3.14t |
| --- | --- | --- | --- | --- | --- |
| 10,000 | 2.7 KB | 0.02–0.03 s | 0.33 / 0.21–0.24 s | 0.056 / 0.060–0.067 s | 0.19 / 0.17–0.31 s |
| 100,000 | 2.7 KB | 0.24–0.27 s | 2.7 / 2.5–3.3 s | 0.50 / 0.74–0.84 s | 2.6 / 1.7–2.7 s |
| 1,000,000 | 2.7 KB | 2.4–3.0 s | 28.0 / 23.3–31.3 s | 5.4 / 5.8–8.6 s | 21.5 / 20.6–32.9 s |

While they waited, one million BEAM processes used 2.7 KB each, about 2.7 GB in total, as reported by `erlang:memory(processes)`. Worker calls completed at about 32,000–43,000 per second on both builds, and that rate did not rise from 3.14 to 3.14t. Own-GIL reached about 116,000–185,000 per second. The worker limit was not traced.

## Own-GIL contexts and common packages

[`subinterpreters.py`](probes/capacity/subinterpreters.py) imports each package in a fresh `concurrent.interpreters` subinterpreter, which applies the same extension check that erlang-python's own-GIL contexts use. Binary wheels were installed from PyPI on 2026-11-17.

| Result on 3.14 | Packages |
| --- | --- |
| Refused: `does not support loading in subinterpreters` | pydantic-core, uvloop, httptools, SQLAlchemy, numpy, pandas, cryptography, lxml, msgspec, grpcio, greenlet, ujson, PyYAML, orjson |
| Loaded | asyncpg, MarkupSafe |
| Not importable | psycopg-binary |

On 3.14t, the same packages were refused in subinterpreters. orjson, psycopg-binary, and grpcio had no 3.14t wheel. Every other package imported with the GIL still disabled, except lxml, which re-enabled it.

Own-GIL contexts cannot load most compiled web and data packages, so this page does not consider them further.

## Moving actor state

[`state/state.escript`](probes/capacity/state/state.escript) keeps a dict of N short strings and an integer counter, and increments the counter once per message. Each figure is the median of 300 messages on one worker context.

- **Pinned:** the dict stays in Python between messages.
- **ETS transfer:** Erlang reads the pickled state from ETS and passes the bytes to Python, which only reads their length.
- **ETS copy:** Python also unpickles, increments, and pickles, and Erlang writes the result back to ETS.

[`state/conventional.py`](probes/capacity/state/conventional.py) measures the same dict as JSON, the way a FastAPI service with a Redis cache would move it: over a stdlib HTTP server on localhost, and through Redis 7 (`GET`, decode, increment, encode, `SET`) over a raw RESP socket. That client is lighter than redis-py.

| Entries | Pickle / JSON size | Pinned, 3.14 / 3.14t | ETS transfer, 3.14 / 3.14t | ETS copy, 3.14 / 3.14t | JSON codec only | JSON over HTTP | JSON through Redis |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 32 | 357 B / 1.1 KB | 27 / 42–45 µs | 28 / 44–45 µs | 35 / 55–58 µs | 12 µs | 474 µs | 131 µs |
| 3,300 | 38.5 KB / 114 KB | 27 / 42–44 µs | 30 / 47–48 µs | 0.58 / 0.59–0.62 ms | 1.0 ms | 1.7 ms | 1.4 ms |
| 33,000 | 418 KB / 1.2 MB | 27 / 44–45 µs | 0.54 ms / 95–99 µs | 8.8 / 6.7–7.8 ms | 13.7 ms | 17.0 ms | 17.3 ms |

The JSON rows ran on 3.14 only.

- **Pinned state costs one call** at every size: 27 µs on 3.14 and 42–45 µs on 3.14t.
- **ETS lookup and byte transfer are cheap.** An ETS lookup took 0.1–0.7 µs, and passing 418 KB to Python added under 0.6 ms.
- **Decoding and encoding the Python objects dominates the cost of moving state.** It grows with the state size on every path.
- **At small sizes, the network adds most of the cost** on the conventional stack: 474 µs over HTTP and 131 µs through Redis, against 35–58 µs through ETS.

## What this measures and what it does not

On this build, a PyBeam actor is not a cheap sequential process. It costs a native thread and about 50–60 KiB, and plain blocking or CPU-bound code in one actor stalls the rest on both 3.14 and 3.14t. A BEAM process costs 2.7 KB. On 3.14t, worker contexts called directly from BEAM processes ran Python in parallel on all 8 CPUs.

Not measured:

- `ActorGroup(execution=ExecutionLimits(...))`. The locked erlang-python `DataActorGroup` accepts only `max_children`.
- Memory or thread limits beyond 10,000 actors, or container cgroup limits below this VM's.
- 3.14t speed on a web request path, and state codecs other than pickle and `json`.
- The cause of the worker-context call rate limit of about 40,000 calls per second.

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

The mode grid and the state probe take the erlang-python application directory, for example `_build/default/lib/erlang_python`. `py_nif.so` there must be built for the Python you are measuring:

```sh
mise exec -- python docs/research/probes/capacity/modes_matrix.py "$PWD/_build/default/lib/erlang_python"
mise exec -- escript docs/research/probes/capacity/state/state.escript "$PWD/_build/default/lib/erlang_python" "$PWD/docs/research/probes/capacity/state"
docker run -d --rm -p 6390:6379 redis:7-alpine
PROBE_KV_PORT=6390 mise exec -- python docs/research/probes/capacity/state/conventional.py
python docs/research/probes/capacity/subinterpreters.py pydantic_core uvloop numpy sqlalchemy
```

Run the last command with the interpreter whose installed packages you want to check.
