# Smoke evidence and comparison status

The default smoke passed three fresh subprocess repeats at contract baseline `af2e257c9bdfdf420f3f523b5d20313f2163f0ab`. The checkout was clean when provenance was captured. Its parent source baseline is `6bd10699823046e94c6444b45904ae5a679a9e0b`. The paired native checkout was clean at `9baab2f4c16e37fab0c98dd3417c61f0d8f4a4d3` and was not modified or executed.

Run command, from this checkout's root:

```sh
uv run --project benchmarks python -m benchmarks run --output benchmarks/results/contract-baseline-af2e257
```

Raw evidence is local under `benchmarks/results/contract-baseline-af2e257/`. It includes source fingerprints, full runtime metadata, three adapter results, 3000 validated receipt JSONL records, resource samples, and cleanup outcomes. Results are ignored by Git. This note does not replace those files.

## What ran

The workload was uniform `rooms.local`, with seed 1, 16 task entities, 100 separate warmup events, and 1000 measured events per repeat. Each entity applied increments and acknowledged their completion. The canonical fixture was the one-byte JSON scalar `1`. The Python transport used in-process objects with no serialization, so no encoded transport byte measurement was claimed.

The runtime was conventional CPython 3.14.10 with the GIL enabled, one interpreter, and one event-loop worker. Pydantic was 2.14.3 and psutil was 7.2.2. The host was macOS 27.2.1 on arm64 with 10 logical CPUs. The recorded one-minute load average was about 1.99. The host was not controlled. No OTP, ERTS, or Elixir runtime was used by this smoke.

Every repeat completed exactly 1000 distinct message IDs with the expected destination and terminal counts. Every repeat observed a queue peak of one and an inflight peak of 16. All 16 entities completed shutdown. Reported failure and loss counts were zero. The guardian found no surviving owned processes and performed no forced process cleanup.

These numbers are diagnostic observations from an uncontrolled smoke, not comparative performance results:

| Repeat | Completed | p50 ns | p95 ns | p99 ns | Creation ns | Deletion ns | Observed adapter RSS bytes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 1000 | 128209 | 267042 | 272125 | 237250 | 91791 | 39550976 |
| 1 | 1000 | 127667 | 266792 | 272083 | 230250 | 90208 | 39518208 |
| 2 | 1000 | 127667 | 265916 | 272125 | 247167 | 90625 | 39534592 |

The diagnostic mean was 96068.14 completed events per second, with sample standard deviation 302.76 across three repeats. CPU samples were observed lifetime lower bounds of about 0.159, 0.156, and 0.158 seconds for the adapter processes. They include startup and warmup. The raw files keep harness and guardian memory and CPU separate. No child VM existed in these Python runs.

The watchdog settings were 15 seconds per repeat, 512 MiB of observed workload-tree RSS, eight processes, 32 threads, and 16 MiB of artifacts. Sampling was requested every 20 ms, with observation overhead in addition to that interval. These were sampled budgets, not physical allocation guarantees. One core was requested but CPU affinity was not enforced on macOS.

## Comparison status

| Backend | Status | Comparable measurements |
| --- | --- | --- |
| Python asyncio smoke | Three correctness repeats passed | The local raw evidence above, with uncontrolled diagnostics only. |
| Actual PyBEAM native actors | Not run | None. Native ownership and loaded build evidence still need validation. Finite actor admission remains unsupported. |
| Native Erlang or Elixir | Not run | None. An independent adapter is still needed. |

There is no evidence here that PyBEAM, Python, Erlang, or Elixir is faster. Other workload families and execution modes have not been measured. No native crash probe, two-node test, long run, integration pipeline, or canonical MVP runtime gate was run in this experiment.

## Checks

Ruff lint and format checks passed. Strict ty passed with warnings treated as errors. All 23 harness tests passed. The tests cover strict boundary parsing, accidental scale rejection, completion and terminal-state rejection, percentile and repeat statistics, advisory locking, wall-time and observed RSS/process/thread/artifact budgets, false cleanup acknowledgments, PID creation-time guards, adapter death, and runner death with guardian-owned detached-child cleanup.

The remaining independent work and ownership requirements are in `CONTRACT.md`. In particular, do not present the managed launcher's unregistered detached VM path as covered by the guardian's spawn ownership guarantee.
