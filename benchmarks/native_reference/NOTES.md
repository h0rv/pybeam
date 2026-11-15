# Native OTP reference

This private adapter uses OTP 29.1.2 and plain `gen_server` rooms. The controller and producer are the Erlang process running `native_bench:main/0`. Each room is a separate `gen_server` process. Python parses the request during setup and replaces itself with `erl` using `os.execv`. The guardian therefore owns the primary VM process without a detached launch. Python does not handle requests or acknowledgments.

No Elixir, PyBeam, or application NIF is loaded. OTP's standard `crypto` module hashes files outside timing and can load its own runtime NIF. The manifest's `pybeam_nif_loaded=false` refers only to the unused PyBeam NIF. The paired native checkout at revision `9baab2f4c16e37fab0c98dd3417c61f0d8f4a4d3` is present for harness provenance only. It is not used to build or run this adapter.

## Build and run

Run these commands from this checkout's root:

```sh
uv run --project benchmarks python -m benchmarks.native_reference --build
uv run --project benchmarks python -m pytest -c benchmarks/pyproject.toml benchmarks/tests/test_native_reference.py -q
uv run --project benchmarks python -m benchmarks run \
  --config benchmarks/native_reference/configs/small.json \
  --output benchmarks/results/native-reference-new-small
```

The trusted adapter command is `benchmarks/.venv/bin/python -m benchmarks.native_reference`. The runner appends the trial directory. Compilation uses one `erlc` invocation with one scheduler. It does not build the NIF. Private BEAM files and their manifest are under `_build/`, which is ignored by Git. The VM verifies the source and BEAM hashes before creating rooms. It retains the manifest in the trial directory.

The small configuration uses two rooms, 17 warmup events, 100 measured events, and a global window of four. The default configuration retains 16 rooms, 100 warmup events, 1000 measured events, and a window of 16. Both use client admission only. The harness retains its 15 second, 512 MiB, eight process, and 32 thread budgets. Every actual run takes the shared exclusive load lock without waiting. All results are uncontrolled smoke observations, not comparative performance claims.

## Supported work and proofs

Only uniform `rooms.local` with the v1 scalar fixture is implemented. The VM prepares both message sequences before creating rooms. It drains warmup replies, snapshots warmup counts, resets the same rooms, verifies zero counts, and then measures. The reset proof records room PIDs, which also appear in the deletion proof.

The producer uses `gen_server:send_request/4` and request collections. It timestamps each event before waiting for admission and records completion when it receives the matching applied reply. Both endpoints use Erlang monotonic nanoseconds relative to one VM-local origin. No timestamp crosses a Python clock boundary. The room increments its count before replying. There is no replay or retry.

The outstanding request collection has one global finite bound. Destination mailboxes remain unbounded. The adapter does not invent a finite mailbox bound or an exact mailbox peak. Integer messages use local BEAM term copying, not ETF serialization. Transport payload bytes and destination queue peak are null.

After all acknowledgments, the controller snapshots every room and stops every room. It awaits each matching worker monitor `DOWN` before recording deletion. JSON receipt construction and serialization happen after the measured phases. Handler failure or timeout aborts the trial and does not produce completed workload evidence. Failure cleanup stops the remaining rooms and awaits their `DOWN` messages before the VM exits.

Other workload families, nonuniform activity, other payloads, finite destination queues, and native crash probes return unsupported. `--probe-failure` is a correctness test hook for a normal Erlang handler exception. It is not a native crash or a scored failure workload.

## Exact shared validation blocker

`contract.Runtime` already permits `native_source_sha=null`. However, `contract.check_correctness()` requires a nonnull value for every non-Python backend. `guardian.supervise()` compares any nonnull value to the paired native checkout revision. That revision would falsely describe this pure OTP implementation.

No shared file was changed. The VM writes a strict v1 `AdapterResult` to `native-result.json` with honest null NIF source identity, plus `native-receipts.jsonl`. It writes `Unsupported` to `adapter.json`, so the runner cannot score this evidence. This is intentional and must not be interpreted as a passing benchmark trial.

The narrow proposed fix requires approval from the shared contract owner. Keep OTP, ERTS, scheduler, executable hash, and loaded BEAM evidence mandatory for Erlang, but require the paired NIF source revision only for PyBeam. The guardian already skips its paired checkout comparison when this field is null. No JSON schema change or guardian change is needed for that fix. After approval, the adapter can publish its existing completion object as `adapter.json` instead of returning this blocker.

## Verified smoke evidence

The final small runner trial is retained at `benchmarks/results/native-reference-verified-b51bb1c/`. It reported OTP `29.1.2`, ERTS `17.1.1`, one scheduler, and two room workers. Python interpreter count and GIL metadata are null. The actual executable was `/opt/homebrew/Cellar/erlang/29.1.2/lib/erlang/erts-17.1.1/bin/beam.smp`, with SHA256 `abf718ce38d03646bbd57239d8ee1d0c91a7d7359e7aba7d4a0861ed8d4248cf`.

The trial produced 100 strict receipts with exact IDs and routes. Terminal counts were `[50, 50]`, warmup counts were `[8, 9]`, and reset counts were `[0, 0]`. Both room monitor shutdowns were observed. The guardian reported zero forced cleanup and no surviving owned processes. Its simultaneous observed process tree RSS peak was 69,844,992 bytes. The outcome was unsupported because of the validation blocker. Throughput, latency, and phase scores in the summary are null.

Four adapter tests passed. They cover strict receipt ingestion, warmup reset including zero warmup, final snapshots, matching room deletion proofs, actual executable and asset hashes, the validation blocker, unsupported capabilities, and cleanup after an actual `gen_server` handler exception in an owned VM. Ruff and ty also passed.

The API references used were the `gen_server.erl` and `json.erl` sources at the official `OTP-29.1.2` tag. In particular, the implementation follows the request collection labels and deletion behavior of `send_request/4` and `receive_response/3`, the serialization and failure behavior of `handle_call/3`, and the atom/binary/null mappings of `json:encode/1` and `json:decode/1`.
