# Corrected handler identity smoke

Source commit `56a6125` fixes the receipt identity blind spot confirmed by independent review `6a164`. The old `d3efc78` observations are provisional and superseded. Their raw records remain unchanged under `results/serial-*-4a2219c`. Their measured rates were actual, but producer labels did not prove the identity of the executing handler.

The corrected Python reference and baseline smoke handlers reply after applying the increment with the received message ID, their own executing entity ID, and applied count. LEGACY raw PyBEAM rooms bind their own identity once during creation and return the same fields from the actual room handler. Producers verify replies and construct receipts from reply fields, not inferred request labels. The existing applied-count check remains. Erlang already bound these identities and was not weakened.

Replies use a frozen internal dataclass, not per-event Pydantic construction. ID and identity fields are protocol envelope metadata. The canonical scalar payload is still 1. Reply construction and verification remain inside measurement and retain actual API overhead. The NIF, guardian, ownership logic, budgets, queue scope, warmup/reset, timestamps, and admission windows were not changed.

## Matching nine trials

All nine corrected trials passed with 100 measured completions each, exact IDs and routes, terminal counts `[50, 50]`, two completed deletions, confirmed cleanup, zero forced cleanup, and zero surviving owned processes. Strict raw receipts match the adapter results. The summaries and native reset/deletion evidence were recomputed locally from the retained records.

Every request used two rooms, 100 measured events, **17 actual warmup events**, uniform activity, seed 1, window 4, queue capacity 4, and `client_admission_only`. Native warmup snapshots were `[8, 9]` and reset snapshots were `[0, 0]`. The `[8, 9]` split is not an eight-event warmup.

All timed work used the shared nonblocking exclusive load lock and ran serially. Budgets stayed at 15 seconds, 512 MiB, eight processes, 32 threads, and 16 MiB artifacts. These remain `uncontrolled_smoke` observations on an unreserved macOS host. Only 100 events were measured, in fixed backend order. There is no controlled performance winner, no capacity claim, and no evidence about the new `@actor` frontend.

| Backend | Trial | Diagnostic completions/s | Latency p50 / p95 / p99, us | Measurement, ms |
| --- | ---: | ---: | --- | ---: |
| Python | 0 | 42,612.12 | 60.500 / 160.666 / 178.333 | 2.346750 |
| Python | 1 | 50,533.76 | 49.792 / 127.333 / 133.917 | 1.978875 |
| Python | 2 | 53,106.74 | 46.750 / 120.041 / 122.958 | 1.883000 |
| LEGACY raw PyBEAM | 0 | 8,505.84 | 438.083 / 1131.875 / 2965.291 | 11.756625 |
| LEGACY raw PyBEAM | 1 | 8,159.02 | 487.583 / 853.834 / 2989.084 | 12.256375 |
| LEGACY raw PyBEAM | 2 | 10,999.49 | 446.166 / 591.791 / 810.792 | 9.091333 |
| Erlang OTP | 0 | 873,682.92 | 2.500 / 16.792 / 18.083 | 0.114458 |
| Erlang OTP | 1 | 838,574.42 | 2.625 / 17.625 / 19.375 | 0.119250 |
| Erlang OTP | 2 | 769,230.77 | 2.750 / 17.667 / 30.250 | 0.130000 |

Mean rates and sample standard deviations were 48,750.88 and 5,469.76 for Python, 9,221.45 and 1,549.56 for LEGACY raw PyBEAM, and 827,162.71 and 53,152.93 for Erlang. Each latency uses only its producer's own clock. Phase durations, sampled RSS, lifetime CPU observations, manifests, hashes, and ownership journals are retained in the raw directories.

Both Python runtimes used GIL CPython 3.14.10. Both native runtimes used OTP 29.1.2, ERTS 17.1.1, and one online scheduler. PyBEAM used the unchanged private native revision `9baab2f4c16e37fab0c98dd3417c61f0d8f4a4d3`. Its two actor PIDs disappeared, native contexts returned from three to one, and room state was collected. Three contexts are not three interpreters or CPU cores. There was one interpreter. CPU affinity was not enforced on macOS.

Python destination queues remain bounded. Native destination mailboxes remain unbounded. Only the requested client admission bound is common. Asset hashes and source identities are checked adapter attestations, not independent proof of VM memory mappings or a security sandbox. Other workload families remain unsupported.

## Real delivery fault controls

`results/corrected-negative-56a6125` retains ten full-guardian negative trials. No result files were edited to manufacture these failures. The probes rotate actual destinations or substitute IDs passed into the real handlers. Python reference probes alter the delivered queue/envelope. Native probes rotate actor references or alter the ID passed to the effect handler inside the actual actor. Received handler replies are retained in `received-replies.json`.

| Adapter | Faults | Guardian result | Adapter exit |
| --- | --- | --- | --- |
| Python reference | Balanced cyclic route rotation, duplicate ID 0, substituted IDs | All three failed | 1 |
| LEGACY raw PyBEAM | Balanced cyclic route rotation, duplicate ID 0, substituted IDs | All three failed | 1 |
| Python baseline smoke | Balanced cyclic route rotation, duplicate ID 0, substituted IDs | All three failed | 0 |
| LEGACY raw PyBEAM | Incorrect applied count after the real effect | Failed | 1 |

Both `python-route/received-replies.json` and `pybeam-route/received-replies.json` record `{id: 0, entity: 0, count: 1}` from the actual handler. Canonical event 0 requested entity 1. Their logs contain `wrong message or executing entity identity` and their outcomes are failed. Duplicate-ID probes retain actual received ID 0 replies; substituted-ID probes retain IDs at least 100.

Reference and PyBEAM identity faults abort on a reply with the wrong received message or executing entity identity. The count fault still aborts with `Invalid applied acknowledgment`. Native failure shutdown remains cooperative. Every negative trial has confirmed cleanup, zero forced cleanup, zero survivors, and null completion, throughput, and latency scores.

The three baseline smoke probes complete all 100 real effects with terminal counts `[50, 50]`. Their actual handler-derived receipts contain the rotated entities, duplicate ID 0, or substituted IDs. The common guardian oracle rejects all three despite the correct aggregate totals. This directly covers the aggregate-count blind spot.

## Reproduction, archive, and limits

From this integration root, run these commands serially with new output names:

```sh
benchmarks/.venv/bin/python -m benchmarks run --config benchmarks/examples/serial_python.json --output benchmarks/results/corrected-python-56a6125
benchmarks/.venv/bin/python -m benchmarks run --config benchmarks/examples/serial_pybeam.json --output benchmarks/results/corrected-legacy-pybeam-56a6125
benchmarks/.venv/bin/python -m benchmarks run --config benchmarks/examples/serial_erlang.json --output benchmarks/results/corrected-erlang-56a6125
uv run --project benchmarks python -m pytest -c benchmarks/pyproject.toml benchmarks/tests -q
```

The standalone test command passed **121 tests in 11.10 seconds**, without an editable package installation or a caller-provided `PYTHONPATH`. The benchmark pytest configuration now supplies its repository/source paths. Ruff check/format and strict ty passed. Regenerating all four wire schemas produced no changes. No type ignores or casts were added.

All seven shipped reference configurations now use this integration root, its benchmark environment, and the private native pair through relative paths. They no longer depend on `bench-python`, `bench-beam`, or `bench-pybeam`.

The three historical directories `native-reference-{final,small,verified}-b51bb1c` were copied from `bench-beam` into `evidence/native-reference-archive` before cleanup. All 45 file sizes and SHA256 hashes were compared to the originals. `evidence/native-reference-archive/manifest.json` retains each proof. Its SHA256 is `5a6d019a039cd3fe67c9984cd60f2b2c075473727644f8ded48a0be4cc4c8477`. Archive tests verify the retained bytes without requiring the old source worktree. Historical paths inside original evidence were not rewritten.

An additional 16-room, 1000-event default-size health attempt was blocked before any trial started because another benchmark held the load lock. There was no retry or bypass. The blocked attempt is retained under `results/corrected-default-health-56a6125`. Parent scheduling is required to complete that additional check. The nine corrected small trials above are complete.

The guardian failure fix has independent acceptance. The corrected reference integration has **no final independent approval** and is not merged into canonical main. Keep `benchmark-integration` and `native-bench-pybeam` for reruns. The three finished source adapter worktrees can now be removed by the parent while retaining their unmerged branches. This task did not delete any worktree.
