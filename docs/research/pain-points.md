# Where the BEAM is an obvious yes for Python

## Verdict

"No locks" and "actors" do not sell PyBEAM. A Python developer on asyncio does not
feel lock pain, and every Python actor library that led with the model stayed niche.
The pains Python developers do feel fall into two groups:

1. **Python cannot do it at all in one process.** Stop a running call, keep other users
   responsive while one request burns CPU, survive a native crash without dropping every
   connection.
2. **Python can do it only by adding infrastructure.** Shared state and broadcast across
   workers (a KV cache and message broker), background jobs (a broker, a worker fleet and a
   result store), presence (a KV cache plus heartbeats).

PyBEAM is an obvious yes only where both groups meet in one app: many long-lived,
stateful connections or sessions that run untrusted or heavy work. Today that is
realtime apps (chat, collaboration, dashboards) and, increasingly, **AI agent sessions**:
long-running, stateful, streaming to a browser, calling slow and flaky tools and native
libraries. The demo should be an agent app, not a counter.

## Proof: four failures of a stock Python server

Each probe runs in the standard library only and has a PyBEAM counterpart that prints
the same lines.

| Pain | Stock Python 3.14 | PyBEAM |
| --- | --- | --- |
| A timeout stops the work | 26 of 30 side effects ran after the timeout | 0 side effects after `terminate()` |
| One CPU-heavy request | 20 of 20 other users froze, up to 3.5 s | 0 of 20 froze; longest wait 11 ms |
| One native crash | Server killed (SIGSEGV), 20 of 20 users disconnected | Only the isolated task died (signal 11); 0 of 20 disconnected |
| State across 4 workers | 400 increments, highest count seen 106; 3 of 8 subscribers notified | One process: highest count 400; 8 of 8 notified |

Rerun from the repository root:

```sh
for probe in timeout_lies loop_stall native_crash worker_state; do
  python3.14 docs/research/probes/painpoints/$probe.py
done
```

PyBEAM counterparts, using the development runtime:

```sh
for probe in timeout_lies loop_stall native_crash worker_state; do
  uv run --locked python docs/research/probes/painpoints/${probe}_pybeam.py
done
```

The PyBEAM stall and crash probes run the heavy work as isolated tasks on conventional
Python 3.14; free-threaded workers were not measured here. Single runs, not averages.

The [chat under load](../../examples/chat_load/README.md) and
[background tasks](../../examples/fastapi_celery/README.md) examples already measure the
same boundaries at application scale: a CPU hog moved other rooms' p99 from 4.5 ms to
2,936 ms on the usual stack and from 3.6 ms to 5.0 ms on PyBEAM; a native crash dropped 9
of 28 other-room sockets versus zero; the job stack ran in 3 processes and 165 MiB
instead of 68 processes and 2.4 GiB.

## Python's pain points, ranked

| Pain | What Python developers do today | What the BEAM gives | Obvious yes? |
| --- | --- | --- | --- |
| Realtime broadcast and presence across workers | KV cache pub/sub, sticky sessions; broadcast silently drops on other workers ([example](https://thecodeforge.io/python/fastapi-websockets/)) | One VM uses every core; process groups, pub/sub and presence are built in | Yes, if the app is realtime |
| Timeouts and cancellation of blocking code | Abandon the thread; it keeps running ([cpython#87185](https://github.com/python/cpython/issues/87185)) | Kill a process; links and monitors tell the owner | Yes, and impossible in plain Python |
| One slow handler stalls the event loop | Move work to threads, processes or a job queue | Preemptive scheduler; Python work on separate loops or processes | Partly: PyBEAM still runs Python in threads, so the gain is isolation, not preemption of Python code |
| Native crash or OOM in an extension | Worker dies with every connection; the process manager restarts it | Crash isolated in a child; the owner gets a DOWN message | Yes for apps that run untrusted or flaky native code |
| Background jobs | Broker + worker fleet + result store + scheduler + dashboard | Supervised tasks in the same process | Yes for in-memory work; durable jobs still need storage |
| Durable long-running workflows | Workflow server or a database-backed library | Supervision plus Mnesia; not durable by itself | No yet: Temporal and DBOS already own this, including Pydantic AI integrations |
| Live introspection of production | Logs, metrics, py-spy | Remote shell, process inspection, tracing | Nice, never the reason to adopt |
| CPU parallelism | multiprocessing, free-threaded 3.14t | Nothing beyond what Python provides | No |
| Distributed compute (ML) | Ray, Dask | Distribution protocol, not a compute scheduler | No: Ray wins on data and GPUs |

## Why earlier attempts stalled

| Project | What it offered | Why it stayed small |
| --- | --- | --- |
| [Pyrlang](https://github.com/Pyrlang/Pyrlang) | A Python process that speaks the Erlang distribution protocol | Useful only if you already run Erlang; Python code got none of the VM's isolation. "Is this still active?" is an open issue since 2022 |
| [Pykka](https://github.com/jodal/pykka), [Thespian](https://thespianpy.com/doc/) | Actor classes on threads or processes | Actors without preemption, isolation or distribution are queues with extra syntax; asyncio covered the same ground. Pykka lives on inside one app (Mopidy) |
| [ErlPort](http://erlport.org) | Call Python from Erlang/Elixir | Solves the other direction (Python inside Elixir) |
| [Faust](https://github.com/robinhood/faust) | Kafka Streams in Python with actor-like agents | Abandoned by its company sponsor; the community fork survives |
| [PyOrleans](https://github.com/antunsz/pyorleans) | Orleans virtual actors in asyncio | New; same gap: semantics without a runtime that enforces them |

What did catch on in Python sold an outcome, not a model:

- **Celery**: "run this later". It became the default despite its operational weight.
- **Ray**: "this function now runs on the cluster", with GPUs and data.
- **Temporal / DBOS**: "your code survives crashes". Both now ship Pydantic AI
  [durable execution integrations](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/).

The lesson for PyBEAM: lead with an outcome a Python developer already wants and
cannot get without a new service, and make the BEAM invisible.

## Is it Python on the BEAM, or the BEAM for Python?

Both are real, for different audiences:

- **Python inside an Elixir app** (call Pydantic AI from Phoenix) is the easier sale:
  Elixir teams already trust the VM and lack Python's ML ecosystem. It is a smaller market.
- **The BEAM inside a Python app** reaches far more developers, but only where the
  outcome is visible in their own app: no extra services, nobody else drops when one
  request fails, a stop button that actually stops.

## Recommended demo

A multi-user agent app in plain Python with Pydantic AI and FastAPI, one process, no
KV cache, broker or workflow server:

1. Each conversation is a supervised session; many users watch and steer the same agent
   live, with presence.
2. A tool call that hangs or loops is stopped by a **Stop** button, and the probe shows
   it actually stopped (no side effects afterwards). In plain Python this is the
   `timeout_lies.py` result.
3. A tool that crashes native code (a bad PDF parser, an OOM) kills only that tool call;
   every other user keeps streaming. In plain Python this is the `native_crash.py` result.
4. A CPU-heavy tool (embedding, parsing) does not freeze other sessions.
5. The same app with a usual stack needs a KV cache, a broker and a job worker; the demo
   shows the process count and memory side by side.

What PyBEAM must not claim in that demo: durable sessions across restarts (needs
storage), preemption of Python bytecode, or cross-machine agents (distribution is two
local nodes today).
