# Local PyBEAM–Ray actor probe

Rerun three fresh-process trials per framework from the repository root:
```sh
bash docs/research/probes/ray/run.sh
```
Uses the installed wheels in `~/probe-env/{pybeam314,ray314}`, each with `psutil`.
Charting needs `~/probe-env/chart314` with `matplotlib`; no mise or native build.
JSON lines, stderr logs, and `comparison.png` go to `~/probe-env/ray-comparison`.
Pass an output directory to `run.sh`; `PROBE_ENVS` overrides the venv parent.

Medians of three trials; these are local microbenchmarks, not production capacity:

| Measurement | PyBEAM | Ray |
|---|---:|---:|
| Cold start to first reply (s) | 0.988 | 2.645 |
| Spawned actors / requested | 1,000 / 1,000 | 91 / 1,000 |
| Spawn + one call each, total (s) | 0.676 | 22.433 |
| Spawn + one call each (µs/actor) | 676 | 246,519 |
| Post-spawn whole-tree RSS (MiB) | 125 | 8,221 |
| Sequential p50 / p99 (µs) | 187 / 584 | 718 / 2,025 |
| Sequential calls/s | 4,833 | 1,193 |
| Concurrent 10,000 calls (s) | 1.893 | 1.770 |
| Concurrent calls/s | 5,282 | 5,649 |
| Final count, every trial, without locks | 10,000 | 10,000 |
| CPU one task / eight tasks (s) | 0.604 / 4.854 | 0.429 / 0.435 |
| CPU speedup: K × single / parallel | 0.989× | 7.896× |

Spawn is serial, awaiting readiness; Ray includes per-spawn resource checks and uses zero-CPU-reservation actors. RSS sums the driver and descendants, double-counts shared pages, and is not peak or proportional memory. Actor counts differ: do not interpret RSS as an equal-N comparison.
Ray targets 1,000 but stops at an 8 GiB tree-RSS budget (91 actors in every trial), 4 GiB available RAM, 8,000 threads, or 90 seconds. This is a conservative safety budget, not a measured maximum. The outer timeout is ten minutes per trial.
Cold start uses the OS process birth timestamp (roughly 10 ms precision), including imports and runtime startup. Calls are 10,000 sequential awaits or 100 callers × 100 awaits. CPU actors are warm and each runs the identical 12-million-iteration integer loop; speedup uses that framework's own single-task baseline. Packaged PyBEAM actors showed no CPU speedup; this says nothing about development free-threaded builds.

## Failure and durability

Run `bash docs/research/probes/ray/run_failure.sh [output-directory]` for three trials.
Needs Docker, `celery314` with `celery[redis]`, and `chart314` with `psutil`.
The runner pulls Redis 7 from `mirror.gcr.io`, keeps the broker alive, isolates trial queues, and saves JSON lines and worker logs.

Actor recovery medians: PyBEAM **2.004 s**, Ray **1.991 s**; both reset state from 9 (99 mid-call) to constructor value 7.
PyBEAM kills a BEAM actor generation with `ActorGroup.crash()` then explicitly rejoins; the caller gets `RuntimeError`. Its diagnostic drains the two-second Python body, so this is not immediate Python cancellation or an OS-process crash.
Ray kills the actor OS process with unlimited restarts and no call retries; the caller gets `ActorUnavailableError`. The next read explicitly polls the same handle through that transient error.

Twenty tasks sleep 0.5 s each, append/fsync per-ID markers, and run at concurrency one. After five markers, SIGKILL stops the captured worker/driver tree; restart never resubmits.

| Framework | Exactly once / twice / missing, each of 3 trials | Killed processes | Recovery observation, median (s) |
|---|---|---:|---:|
| PyBEAM | 20 / 0 / 0 | 3 | 10.196 |
| Celery | 20 / 0 / 0 | 2 | 9.841 |
| Ray | 5 / 0 / 15 | 9 | 14.124 |

PyBEAM restarts launcher/guardian/BEAM on the same local Mnesia directory with default replicas and a 2 s lease; host/disk loss is untested. Celery restarts its prefork worker and child against surviving Docker Redis, using late ack, reject-on-worker-lost, 2 s visibility, and 1 s restore sweep. Neither promises exactly-once effects; an earlier shared-queue run produced one duplicate, with cross-trial delivery not excluded.
Ray restarts a fresh local cluster without external queue state: five existing marker effects survive, but the remaining fifteen tasks do not resume during 12 s after readiness. Counts are observations, not power-loss or partition guarantees. PyBEAM/Celery observation includes startup, remaining work, and 2 s settling; all captured trees have zero live processes after killing.

## Appendix

Linux VM: `nproc=8`, RAM 31.34 GiB; CPython 3.14.10, live GIL enabled.
Wheels: `python-beam==0.1.0a1`, `ray==2.60.0`; `psutil==7.2.2`, `matplotlib==3.11.2`.
Failure probe adds `celery==5.7.0`, `redis==8.1.0`, and a Docker Redis 7 broker (no broker disk persistence).
