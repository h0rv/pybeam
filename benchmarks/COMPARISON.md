# Historical serial reference smoke, superseded

These observations from `d3efc78` are provisional and superseded by `CORRECTED_COMPARISON.md`. Independent review `6a164` found a correctness proof blind spot. Python and LEGACY raw PyBEAM fabricated receipt identities from producer request labels. Balanced cyclic misrouting and duplicate delivered IDs could therefore pass despite terminal counts `[50, 50]`. The earlier exact-route claim was invalid. Do not use these records as corrected correctness acceptance or as evidence for the new `@actor` API.

The original raw records under `results/serial-*-4a2219c` remain unchanged. Their measured rates and repeat statistics are actual historical observations; the defect was in the completion proof. Nine trials were originally accepted at source commit `4a2219c38ec461a9971bad7992ed1bd5ef247bc4`. Each completed 100 events with the reported terminal totals, entity deletion totals, confirmed process cleanup, zero forced cleanup, and zero surviving owned processes. Strict receipt parsing alone did not establish actual handler identity.

These are uncontrolled smoke observations. Other builders were active in this work period. There was no reserved idle host. The very short measured intervals and fixed backend order also limit interpretation. There is no controlled performance winner and no production capacity claim.

The guardian follow-up from `1f5c767` is included privately. Its independent review was still pending when this report was written. Passing these trials does not constitute release approval.

## Common request

All three configurations request uniform `rooms.local`, two rooms, 17 warmup events, 100 measured events, seed 1, window 4, queue capacity 4, and `queue_bound_scope="client_admission_only"`. The payload fixture is scalar increment 1. Delivery is exactly once without retries. Budgets remain 15 seconds, 512 MiB observed process tree RSS, eight processes, 32 threads, and 16 MiB artifacts per trial.

Fixtures are prepared before entity creation. Warmup drains before the same entities are reset. Warmup duration includes snapshot and reset work. Terminal snapshots occur after measurement and before deletion. Every producer timestamps before waiting for client admission. No producer creates an unbounded backlog of tasks. All measured inflight peaks were four.

The requested client admission bound is the only common queue guarantee. Python's destination queues are also bounded, with observed peak two. Legacy PyBEAM and Erlang destination mailboxes remain unbounded and report no destination queue peak. No finite native mailbox or new production admission API is claimed.

Python uses tasks and queues in one event loop. Legacy PyBEAM uses actual raw `ActorSpec` actors and retained Python integer objects. Erlang uses actual `gen_server` requests and local integer term copying. None of these transports uses per-event JSON or Pydantic parsing. Native calls retain their actual transport and reply overhead. Python snapshots shared state directly, while native snapshots use their real call APIs. These are semantic comparisons of the requested work, not identical instruction streams.

Each latency uses two timestamps from its own producer's monotonic clock. Only durations are compared across backends.

## Observations

Rates are diagnostic completed events per measured second. Latency columns are p50, p95, and p99 in microseconds. RSS is the sampled simultaneous adapter plus descendant resident set in bytes, including startup and warmup. It is not a measurement-phase allocation total.

| Backend | Trial | Rate | Latency p50 / p95 / p99, us | Measurement, ms | Tree RSS, bytes |
| --- | ---: | ---: | --- | ---: | ---: |
| Python | 0 | 38,460.31 | 62.916 / 160.583 / 201.875 | 2.600083 | 37,339,136 |
| Python | 1 | 39,864.46 | 62.834 / 157.250 / 157.750 | 2.508500 | 36,438,016 |
| Python | 2 | 39,737.73 | 62.458 / 157.875 / 165.917 | 2.516500 | 39,010,304 |
| LEGACY raw PyBEAM | 0 | 6,714.34 | 711.666 / 978.000 / 1135.041 | 14.893500 | 158,924,800 |
| LEGACY raw PyBEAM | 1 | 7,462.15 | 642.958 / 812.583 / 852.625 | 13.400958 | 158,679,040 |
| LEGACY raw PyBEAM | 2 | 6,869.49 | 710.291 / 958.625 / 1019.583 | 14.557125 | 158,580,736 |
| Erlang OTP | 0 | 574,026.45 | 4.916 / 21.583 / 22.750 | 0.174208 | 83,034,112 |
| Erlang OTP | 1 | 574,300.07 | 4.916 / 21.625 / 22.917 | 0.174125 | 83,542,016 |
| Erlang OTP | 2 | 577,200.58 | 4.917 / 21.041 / 23.125 | 0.173250 | 83,050,496 |

The mean diagnostic rates and sample standard deviations were 39,354.17 and 776.69 for Python, 7,015.33 and 394.66 for LEGACY raw PyBEAM, and 575,175.70 and 1,758.92 for Erlang. These numbers do not establish which backend performs best under controlled conditions.

Creation, warmup, and deletion durations are preserved separately in every `adapter.json` and `summary.json`. PyBEAM deletion took about 207 to 210 ms and is not hidden in measurement throughput. Native VM startup is outside the entity phases and is recorded separately for PyBEAM. Resource samples cover the whole owned process lifetime. Sampled CPU is a lower bound on lifetime CPU, not exact measured-phase CPU. RSS may count shared pages more than once. Very short peaks can occur between 20 ms samples.

## Actual runtimes and cleanup evidence

Python and embedded PyBEAM used conventional GIL CPython 3.14.10. Python reported one interpreter and one event-loop worker. PyBEAM reported one interpreter, three active native worker contexts, and two native actor PIDs. Contexts returned from three to the baseline one after deletion. Actor PIDs disappeared and Python room state was collected. Three worker contexts do not mean three interpreters or CPU cores.

Both native runtimes reported OTP 29.1.2, ERTS 17.1.1, and one online scheduler. Erlang reported two room workers and null Python interpreter/GIL fields. This macOS host did not enforce CPU affinity. One requested core and one OTP scheduler do not prove exclusive CPU use.

The actual native executable was `/opt/homebrew/Cellar/erlang/29.1.2/lib/erlang/erts-17.1.1/bin/beam.smp`, SHA256 `abf718ce38d03646bbd57239d8ee1d0c91a7d7359e7aba7d4a0861ed8d4248cf`. The Python executable SHA256 was `5504147b44ff4544bb4f1039d2b4a7bed66aac3cfa4534471e32ef559687f28d`.

PyBEAM used the existing private native build at `../native-bench-pybeam`, pinned to `9baab2f4c16e37fab0c98dd3417c61f0d8f4a4d3`. Its actual `priv/py_nif.so` SHA256 was `6492f8ad58938c36f2480ca18bad52a0f713873eab646c666068b59515a7009b`. No native source or production runtime was changed. The build manifest and all declared assets were reverified after the trials. Erlang's two adapter BEAM files were built inside this integration worktree. Its build manifest contains actual compiler, source, and asset paths and hashes. It does not invent an unused NIF revision.

Every native trial recorded warmup counts `[8, 9]` and reset counts `[0, 0]`. Erlang retained matching room monitor `DOWN` proofs. PyBEAM retained actor/context inventories, collected-state evidence, the guardian-owned VM identity, and VM exit before the cleanup acknowledgment. All nine ownership journals were rechecked for confirmed cleanup and no live registered identities.

The guardian owns the actual ERTS process before work. Erlang replaces the primary adapter with ERTS. PyBEAM uses the existing guardian spawn broker before entering the existing `py_launch` path. This is not the unsupported normal managed-host detached launch path. Asset checks are adapter attestations checked against file bytes and checkout identities, not independent proof of VM memory mappings or a security sandbox.

This PyBEAM result is LEGACY raw actor prototype evidence only. It says nothing about the new `@actor` safe-data frontend, which is a separate API pending review.

## Commands and artifacts

Run from `/Users/robby/projects/worktrees/pybeam/benchmark-integration`. These commands ran sequentially, never in parallel. Each runner acquired the global nonblocking exclusive load lock. A busy lock would fail visibly without retry. Choose new output names when repeating them.

```sh
benchmarks/.venv/bin/python -m benchmarks run --config benchmarks/examples/serial_python.json --output benchmarks/results/serial-python-4a2219c
benchmarks/.venv/bin/python -m benchmarks run --config benchmarks/examples/serial_pybeam.json --output benchmarks/results/serial-legacy-pybeam-4a2219c
benchmarks/.venv/bin/python -m benchmarks run --config benchmarks/examples/serial_erlang.json --output benchmarks/results/serial-erlang-4a2219c
```

Each directory retains strict adapter results, raw typed receipts, resource samples, provenance, ownership journals, outcomes, and repeat summaries. The native directories also retain actual build manifests and reset/deletion evidence. Source provenance was clean and identical across the three runs.

## Validation and remaining limits

The full benchmark suite passed 103 tests in 9.19 seconds. It includes the existing 31 guardian/contract tests and the new references and oracle negative controls. Ruff check and format passed from `benchmarks/`. Strict ty passed with the repository, `src`, and `typings` search paths. Regenerating all four JSON schemas produced no changes. This integration did not rerun the showcase UI or browser suite.

Negative controls cover missing or duplicate acknowledgments, wrong terminal state, missing deletion or cleanup, missing BEAM/source evidence, false backend identity, missing PyBEAM native revision or NIF, mismatched native revision, changed NIF hash, and changed executable/BEAM/source bytes. The raw PyBEAM producer also rejects a bad applied-count reply instead of recording a completion.

Fault, churn, bridge, execution-mode, distribution, other payload, and nonuniform families remain unsupported. No broader correctness or performance conclusion follows from these nine small trials.

The source worktrees `bench-python`, `bench-beam`, and `bench-pybeam` are no longer dependencies after the follow-up configuration portability fixes and evidence archive. Their changes are committed here and raw trials are retained here. This task does not delete them. Keep `benchmark-integration` and the private `native-bench-pybeam` assets for reruns. The completed trial records no longer require the original three adapter worktrees.
