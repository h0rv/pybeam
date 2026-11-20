# PyBEAM benefits and tradeoffs

PyBEAM fits applications with long-lived connections that share state and run
crash-prone or blocking work. Isolated tasks keep that work outside the serving
process. Shared state and pub/sub avoid a separate broker for this local,
in-memory workload. Python applications can also isolate work in child processes;
these probes compare configurations, not capabilities exclusive to PyBEAM.

## Four-worker fault and state probes

Each range gives the minimum–maximum across three runs, not a confidence
interval. Baseline and PyBEAM batches run separately on the same VM. Twenty
persistent WebSocket clients observe faults in four Uvicorn workers. The
runaway case uses four Gunicorn Uvicorn workers with a ten-second timeout.
The kernel assigns connections without balancing their counts.

| Measurement | Four-worker baseline | Source-build PyBEAM |
| --- | --- | --- |
| Native crash: other clients disconnected | 1–14 of 20; all on the fault worker | 0 of 20; isolated child exits with signal 11; server remains responsive |
| CPU hog: maximum successful ping latency | 3000.20–3000.64 ms | 7.99–11.70 ms |
| CPU hog: pooled successful-ping p99 | 1.85–4.31 ms | 3.38–5.76 ms |
| CPU hog: clients waiting more than one second | 3–7 of 20; all on the fault worker | 0 of 20 |
| Runaway worker: fault connection closes after submission | 10.713–10.922 s; each log contains `WORKER TIMEOUT` | Explicit `terminate()` returns in 16.08–33.29 ms |
| Other outstanding pings lost during runaway worker termination | 0–8; the fault worker has 0–8 observer clients | Not measured by the termination probe |
| Effects after termination request / after return | Not measured by the Gunicorn probe | 0 / 0 in each of three runs; six total effects per run |
| Highest count after 400 increments | 188–286 | 400 in every run |
| Subscribers receiving one publication | 1–5 of 8 | 8 of 8 in every run |

The [baseline](probes/painpoints/uvicorn_faults.py) records worker PIDs,
fault-worker occupancy and dropped-client PIDs. A crash drops that worker's
clients, not a predictable quarter of all clients. Successful-reply p99 can hide
a few severe stalls and excludes lost replies.

The [PyBEAM CPU probe](probes/painpoints/loop_stall_pybeam.py) uses a
40-million-iteration loop in an isolated child and stdlib TCP clients. The
baseline uses a three-second pure-Python loop and WebSockets. These are isolation
probes, not equal-work throughput comparisons or evidence of embedded worker CPU
parallelism.

The [termination probe](probes/painpoints/timeout_lies_pybeam.py) appends timestamped
effects every 100 ms and observes the file for three seconds after termination
returns. It records request and return timestamps. Termination does not undo
effects. A separate three-run batch records 0–1 effects after the request without
measuring the return boundary; zero effects during the request-to-return interval
is not a guarantee. Native termination emits `child_exited,{signal,9}` diagnostics
even when the probe completes successfully. Gunicorn kills a worker; PyBEAM
terminates an isolated call. The PyBEAM termination probe does not measure
collateral client loss. Gunicorn timing measures connection loss, not an exact
OS process-death timestamp.

The state baseline sends 400 sequential increments over new connections, then
publishes once to eight persistent subscribers. Process-local counters and lists
split across workers; this is not an increment race. The
[PyBEAM counterpart](probes/painpoints/worker_state_pybeam.py) submits 400 concurrent
atomic increments through one VM's services. Neither probe establishes
persistence or cross-host recovery.

## Rerun

From the repository root, run three fresh instances per case:

```sh
for case in crash hog runaway state; do
  for trial in 1 2 3; do
    uv run --script docs/research/probes/painpoints/uvicorn_faults.py "$case"
  done
done
```

The baseline declares pinned dependencies in inline script metadata. For PyBEAM,
prepare the [development runtime](../development.md#use-a-development-runtime),
set `[tool.pybeam].development_runtime` to the built profile, and put a compatible
`erl` on `PATH`. Then run:

```sh
for probe in native_crash loop_stall timeout_lies worker_state; do
  for trial in 1 2 3; do
    uv run --locked python "docs/research/probes/painpoints/${probe}_pybeam.py"
  done
done
```

Each successful probe emits one JSON result. Native diagnostics can accompany
it. The retained stock-Python scripts use stdlib TCP or threads; they are not the
four-worker comparison in the table.

## Application-scale evidence

- [Chat under load](../../examples/chat_load/README.md) compares an eight-worker
  web stack with PyBEAM. CPU-fault other-room p99 medians are
  4.52 → 2935.94 ms versus 3.63 → 4.95 ms. This does not establish zero latency impact.
- [Background tasks](../../examples/fastapi_celery/README.md) reports median idle
  PSS of 164.7 MiB in three processes versus 2355.9 MiB in 68 processes, including
  64 Celery workers. The port loses tasks and results on process death; the
  original stack retains queued work across worker restarts.
- [Agent sessions](../../benchmarks/AGENTS_COMPARISON.md) exercise replicated
  sessions and isolated tools. Same-host node loss is not host-loss or partition
  tolerance. An interrupted tool without a committed result is not rerun;
  at-most-once execution does not imply no external effect.
- [Two-node pub/sub and presence](../guide/integrations/pyview.md#send-posts-between-two-local-nodes)
  demonstrate local distribution without a broker. Presence is eventually
  consistent; disconnected clients miss messages without replay.
- [Ray actor probes](probes/ray/README.md) report three-trial medians of 1000
  PyBEAM actors at 125 MiB tree RSS versus 91 Ray actors at 8221 MiB. Ray stops at
  a safety budget, not a measured capacity ceiling. Actor counts differ and RSS
  double-counts shared pages. Packaged PyBEAM shows no CPU speedup in that probe;
  Ray provides nearly eightfold speedup for eight CPU tasks.

## Boundaries

Shared memory, supervision and pub/sub do not supply durable jobs or state.
Applications own storage, authorization, overload and uncertain outcomes.
Isolated tasks protect the serving interpreter from native crashes; embedded
Python code has no such process boundary. Shared-loop asyncio does not imply
multicore execution, and the BEAM scheduler does not preempt Python bytecode.
See the [execution model](execution-model.md).

The [HTTP comparison](../../benchmarks/web/RESULTS.md) puts Hornbeam median
throughput 12–20% below tuned single-worker Uvicorn and 28–38% below Granian at
the tested concurrencies. These shortfalls are not equivalent percentages of
peer speedup. The [socket benchmark](../../benchmarks/web/SCALE_RESULTS.md)
records 1.8–2.1 s p99 echo latency in clean conventional-Python runs at 20000
connections. A separate forced full collection consumes 1.1 s CPU and reclaims
no objects; it does not establish GC as the sole cause of echo delays.

## Appendix: measured environment

Fault/state probes use conventional CPython 3.14.10, Linux x86-64 and OTP 28.3.
The development profile is `/home/ubuntu/repos/pybeam/_build/hornbeam`.
Source pins are erlang-python `6d51fc2cb39c74384393b71fdb4781b7622e9495` and
Hornbeam `d407bfa8db89c71d5868c3e95745ecd4f30ed7cc`; rebar is 3.25.1.
Baseline dependencies are FastAPI 0.165.0, Uvicorn 0.54.0, Gunicorn 27.0.1,
uvicorn-worker 0.4.0 and websockets 17.4. The three explicit return-boundary
termination trials use the same build as the other PyBEAM probes.
Application-scale and Ray measurements have their own environments in the
linked sources; they are not measurements from this fault-probe batch.
