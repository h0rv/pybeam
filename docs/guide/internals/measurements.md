# Measurements

These numbers come from probes on one machine and vary with hardware and load. They are not production limits.

- Linux x86-64, 8 CPUs, 31 GiB memory.
- CPython 3.14.10 (GIL enabled) and 3.14.10t (free-threaded), Erlang/OTP 29.1.2, erlang-python `d60392c`.
- Method and raw data: [actor capacity](../../research/actor-capacity.md). The fan-out and blast-radius sections come from the earlier [execution modes](../../research/execution-modes.md) probes on erlang-python `c007eb5`.

## Contexts

Each mode was measured with BEAM processes calling erlang-python contexts directly. The call is an empty round trip.

| Mode | Create one | Memory per context, 3.14 / 3.14t | Call p50, 3.14 / 3.14t | `os.abort()` in one of 4 |
| --- | --- | --- | --- | --- |
| `worker` | 1.1–1.2 ms | under 0.1 MiB / under 0.1 MiB | 28–36 / 41–42 µs | The VM exits |
| `isolated` | 61–132 ms | 21–22 / 26–27 MiB | 52–401 µs | The other 3 survive; a replacement is ready in 64–134 ms |

A 3,000,000-step integer loop takes about 0.33 s alone on either build:

| Mode | 8 jobs, 3.14 | 8 jobs, 3.14t | 64 jobs, 3.14 | 64 jobs, 3.14t |
| --- | --- | --- | --- | --- |
| `worker` | 2.86 s | 0.33–0.43 s | 22.9 s | 2.68–2.77 s |
| `isolated` | 0.35 s | 0.32–0.64 s | 2.31 s | 2.31–2.70 s |

On 3.14t, worker contexts ran on all 8 CPUs. On 3.14 they took turns. `time.sleep` overlapped in every mode on both builds, because each context has its own thread.

## Actors

Each actor owns a worker context and its method bodies run on the shared event loop.

| 10,000 of | Memory added | OS threads | Start time |
| --- | --- | --- | --- |
| PyBEAM actors | about 584 MiB | 10,000 | about 24 s |
| `threading.Thread` blocked on an `Event` | 206 MiB | 10,000 | 1.2 s |
| asyncio tasks blocked on an `Event` | 8.5 MiB | 0 | 0.03 s |

On 3.14t, 10,000 actors added 494 MiB. Spawning did not fail at 10,000, the last checkpoint.

N actors are called at once with `asyncio.gather`. The CPU job is a 2,000,000-step loop that takes about 0.12 s alone.

| Method body | N | 3.14 | 3.14t |
| --- | --- | --- | --- |
| `await asyncio.sleep(0.05)` | 100 | 0.071–0.086 s | 0.10 s |
| `await asyncio.sleep(0.05)` | 8,000 | 2.8–4.3 s | 12.8 s |
| `time.sleep(0.05)` | 32 | 1.62 s | 1.65 s |
| `await asyncio.to_thread(time.sleep, 0.05)` | 32 | 0.16 s | 0.17 s |
| CPU job | 8 | 0.96 s | 1.06 s |
| `await asyncio.to_thread(cpu_job)` | 8 | 1.0 s | 0.17 s |
| Empty method, 1,000 calls in a row on one actor | 1 | 0.41–0.82 ms per call | 0.61 ms per call |

- Blocking and CPU work serialized every actor on both builds, because all method bodies share one loop thread.
- `asyncio.to_thread` restored overlap, up to the default executor's 12 threads on 8 CPUs.
- 10,000 concurrent calls from one caller loop failed with `RuntimeError: Native callback queue full`. erlang-python reserves at most 8,192 native callbacks per loop.

## Fan out: four CPU jobs

Each row runs the same four pure-Python jobs at once. Bars show when each job ran.

![Four CPU jobs on a shared time axis for asyncio, threads on 3.14 and 3.14t, a process per job, a prefork pool, and PyBEAM worker, isolated and separate-node modes, with wall time and speedup per row.](../assets/fan-out.svg)

With the GIL, asyncio, threads and `worker` ran the jobs one at a time. On 3.14t, threads and `worker` overlapped them. `isolated`, a process per job, a prefork pool and separate nodes overlapped them on either build.

## Blast radius: one job calls `os.abort()`

Four jobs run; one aborts its interpreter.

| Lane | Other jobs that finish | Replacement ready in |
| --- | --- | --- |
| asyncio, threads | 0 of 3: the process dies | Restart the app |
| `worker` | 0 of 3: the VM dies | Restart the app |
| Process per job | 3 of 3 | 80 ms for a new process |
| Prefork pool | 0 of 3: the pool breaks | 53 ms for a new pool |
| `isolated` | 3 of 3 | 70 ms for a new child |
| Separate nodes | 3 of 3 | 254 ms for a new node |

## Fan in: many BEAM processes, 8 contexts

N BEAM processes each make one empty call on one of 8 contexts, with at most 32 calls in flight.

| Processes | Memory per process | Spawn all | `worker`, 3.14 / 3.14t | `isolated`, 3.14 / 3.14t |
| --- | --- | --- | --- | --- |
| 10,000 | 2.7 KB | 0.02–0.03 s | 0.33 / 0.21–0.24 s | 0.19 / 0.17–0.31 s |
| 100,000 | 2.7 KB | 0.24–0.27 s | 2.7 / 2.5–3.3 s | 2.6 / 1.7–2.7 s |
| 1,000,000 | 2.7 KB | 2.4–3.0 s | 28.0 / 23.3–31.3 s | 21.5 / 20.6–32.9 s |

An earlier probe compared asyncio: 100,000 asyncio tasks used 1.1 KB each and finished 100,000 empty calls in 429 ms, against 2.59 s on 3.14 and 1.80 s on 3.14t for BEAM processes calling worker contexts.

One million waiting BEAM processes used about 2.7 GB. Worker calls completed at about 32,000–43,000 per second on both builds; the cause of that ceiling was not traced.

## Not measured

- Throughput of a web request path on 3.14t.
- Actors beyond 10,000, or memory limits set by a container.
- Actor groups with `ExecutionLimits`.
