# Native OTP reference

This private adapter uses OTP 29.1.2 and plain `gen_server` rooms. The Erlang process running `native_bench:main/0` produces the requests. Each room is a separate `gen_server` process. Python parses the request during setup and replaces itself with `erl` using `os.execv`. The guardian owns the primary VM process. Python does not handle requests or replies.

No Elixir or PyBEAM application NIF is loaded. OTP's standard `crypto` module hashes files outside timing and can load its own runtime NIF. The manifest's `pybeam_nif_loaded=false` refers only to the unused PyBEAM NIF. The paired native checkout is recorded by the harness but is not used to build or run this adapter.

## Build and run

Run these commands from this checkout's root:

```sh
benchmarks/.venv/bin/python -m benchmarks.native_reference --build
PYTHONPATH=.:src benchmarks/.venv/bin/python -m pytest -c benchmarks/pyproject.toml benchmarks/tests/test_native_reference.py -q
benchmarks/.venv/bin/python -m benchmarks run --config benchmarks/examples/serial_erlang.json --output benchmarks/results/serial-erlang
```

Compilation uses one `erlc` invocation with one scheduler. It does not build the NIF. Private BEAM files and the build manifest are under `_build/`, which Git ignores. The VM verifies source and BEAM hashes before creating rooms and saves the manifest in the trial directory.

The matching small configuration uses two rooms, 17 warmup events, 100 measured events, and a global window of four. It requests client admission only. Budgets remain 15 seconds, 512 MiB, eight processes, and 32 threads. Every trial uses the shared exclusive load lock without waiting. Results are uncontrolled smoke observations, not evidence of a performance winner.

## Supported work and proofs

Only uniform `rooms.local` with the scalar fixture is implemented. The VM prepares both message sequences before creating rooms. It drains warmup replies, snapshots counts, resets the same rooms, and verifies zero counts before measurement. The reset proof records room PIDs, which also appear in the deletion proof.

The producer uses `gen_server:send_request/4` and request collections. It timestamps each event before waiting for admission and records completion after receiving the matching applied reply. Both endpoints use Erlang monotonic nanoseconds relative to one VM origin. No timestamp crosses a Python clock boundary. Each room increments its count before replying. There is no retry.

The outstanding request collection has one global bound. Destination mailboxes remain unbounded. There is no finite mailbox claim or exact mailbox peak. Integer messages use local BEAM term copying, not ETF serialization. Transport payload bytes and destination queue peak are null.

After all replies, the controller snapshots every room and stops every room. It awaits each matching worker monitor `DOWN` before recording deletion. JSON receipt construction and serialization happen after the measured phases. A handler failure or timeout aborts the trial. Failure cleanup stops the remaining rooms and awaits their `DOWN` messages before the VM exits.

Other workload families, nonuniform activity, other payloads, finite destination queues, and native crash probes return unsupported. `--probe-failure` tests a normal Erlang handler exception. It is not a native crash or a scored failure workload.

## Runtime evidence

The original adapter returned unsupported because the shared oracle incorrectly required an unused paired NIF revision. The integration fix requires that revision and NIF assets for PyBEAM, but not for pure OTP. Pure OTP must supply its actual OTP and ERTS versions, scheduler count, executable hash, and adapter BEAM and source hashes. Elixir also requires its actual version. A pure OTP result that declares Python or the PyBEAM NIF is rejected.

The guardian checks asset paths against the declared checkout and hashes the actual files. These are checks of adapter attestations, not independent proof of VM memory mappings. No schema or production runtime change is required. `adapter.json` now contains the same strict completion evidence as `native-result.json`.

The adapter follows the `gen_server.erl` and `json.erl` sources at the official `OTP-29.1.2` tag. The relevant APIs are `send_request/4`, `receive_response/3`, `handle_call/3`, `json:encode/1`, and `json:decode/1`.
