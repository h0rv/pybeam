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

## Appendix

Linux VM: `nproc=8`, RAM 31.34 GiB; CPython 3.14.10, live GIL enabled.
Wheels: `python-beam==0.1.0a1`, `ray==2.60.0`; `psutil==7.2.2`, `matplotlib==3.11.2`.
