# Shared benchmark contract, version 1

Only correctness-passing trials receive latency and throughput scores. This experimental contract covers uniform scalar `rooms.local` work, not production capacity, new PyBEAM APIs, or the MVP runtime gate.

## Scope and defaults

Defaults are 16 entities, 100 warmup events, 1000 measured events, three fresh subprocess repeats, and a 15 second deadline per repeat. The default workload process tree has a 512 MiB observed RSS budget, eight processes, 32 threads, and 16 MiB of artifacts. The guardian and harness have separate resource observations. They are not included in that workload budget.

Use `--config /path/config.json` for an explicit configuration and `--output /tmp/unique-directory` for another artifact directory. A schema can be used by an Erlang or Elixir adapter without importing Python. A larger request requires `scale="explicit_progressive"`. Increase sizes in small steps only after checking the previous run. There is no automatic extrapolation or automatic stress ramp.

## Adapter interface

`contract.py` is the authoritative typed wire format. `schemas/` contains its generated JSON Schemas. Every model rejects unknown keys and coercion. Configurations and adapter results include a schema version.

The runner starts `adapter_command` with the absolute trial directory appended as its final argument. An empty command selects `python_smoke.py`. The command is an explicitly trusted executable, never shell text. The working directory is this checkout's root.

The adapter must do the following:

### Reject unsupported requests

Parse `config.json` and reject unsupported semantics by writing an `Unsupported` object to `adapter.json`. Exit successfully for this outcome. Do not substitute a different implementation or return zero measurements.
### Separate creation, warmup and measurement

Create entities and record the creation duration. Run warmup separately. Drain warmup acknowledgments and reset state before measurement.

Keep the same entities alive across those phases. The smoke records raw observations using small internal dataclasses. Pydantic result construction and JSON serialization happen after the measured phases. Deletion means completed worker shutdown, not a claim that the Python allocator returned memory to the OS.
### Use the canonical scalar sequence



Use `messages(workload, count)` for version 1 scalar work. Message IDs are zero through count minus one. The entity is `(id + seed) % entities` and the increment is one. Warmup IDs restart at zero. The canonical payload fixture is the UTF-8 JSON scalar `1`, exactly one encoded byte. Prepare the deterministic sequence and payload fixtures before entity creation.

Keep transport work inside measurement when a backend actually performs it. `transport_encoding` and `transport_payload_bytes` describe that backend's actual transport, separately from the canonical fixture. The Python smoke passes Python objects through in-process queues without serialization, so its encoded transport byte count is null, not a fictitious byte measurement.

Transport envelopes are additional bytes and must not be described as payload bytes.
### Prove handler identity after the effect

Bound outstanding work and record actual queue and inflight peaks if observable. Admission waiting counts toward sender latency. A completion means the increment was applied, not merely submitted. The real handler must reply after the effect with the received message ID and its own executing entity identity.

Producers verify these against the request and populate receipts from actual reply fields. Balanced misrouting must fail even when aggregate terminal counts remain correct. These identity fields are protocol envelope metadata, not extra bytes in the canonical scalar payload. No per-event Pydantic construction is required.

Do not retry implicitly.
### Use one clock domain

Record send and acknowledgment timestamps in the same sender's monotonic nanosecond clock. Erlang native callers must calculate both endpoints in their own clock domain. Never compare Python absolute monotonic timestamps to Erlang timestamps. Cross-backend comparisons use durations only.
### Drain, delete and attest the loaded runtime



Drain all acknowledgments, inspect every terminal entity count, then delete all entities. Record deletion separately. Write the result only after cleanup. Report the runtime actually used, including its full version, executable hash, execution mode, interpreter and worker counts, GIL state when applicable, and actual OTP scheduler count.

A PyBEAM adapter must identify the paired native source revision it loaded. The guardian checks that revision against the paired checkout. PyBEAM results must supply `loaded_assets` with hashes of the actual NIF and BEAM files. Pure Erlang or Elixir does not load that NIF and must not claim its revision.

Instead it must provide the actual OTP and ERTS versions, scheduler count, ERTS executable hash, and adapter BEAM and source hashes in `loaded_assets`. Elixir also requires its actual version. The guardian verifies file hashes and requires adapter assets within the declared implementation checkout. Retain the loaded build manifest as an artifact too.

These are adapter attestations, not proof from inspecting the VM's mappings.
### Enforce the deadline

Exit within the trial deadline. A nonzero exit, invalid evidence, budget breach, or surviving owned process fails the trial. Failure and unsupported results have no throughput or latency score.

`check_correctness()` rejects missing, duplicate, or misrouted acknowledgments, wrong terminal counts, missing deletion, unexpected failure or loss, exceeded instrumented bounds, and a measurement shorter than its completion span. It is deliberately limited to uniform `rooms.local` and the scalar payload above. The other family IDs reserve names, not working implementations or correctness oracles. Extend the typed config, proof fields, and oracle together before accepting their results.

`queue_bound_scope="destination_queue"` requests a bound on each actual destination queue. `client_admission_only` instead describes a global caller admission bound and does not claim a finite native mailbox. PyBEAM currently supports explicit unbounded actor mailboxes only. A PyBEAM adapter must return unsupported for a finite destination queue request. A comparison using only client admission must use that same scope on every backend and record the remaining mailbox difference. No production admission API may be invented to make a benchmark pass.

## Process ownership and resource controls

The runner owns an independent guardian session. The guardian owns the adapter session and keeps the load lock open. It checks the runner's PID and creation time, not only its PID. It retains identities for observed descendants, including descendants in other process groups. Cleanup signals only matching identities. It does not kill a global process name, a user process group, or an unrelated VM.

Detached runtime creation must use `ownership.spawn_owned(trial_directory, argv)`. The guardian starts that process directly in a fresh session, retains its PID and creation time before replying, and owns its cleanup even if the adapter or runner dies. Calls to this setup helper are serial. An adapter must gracefully stop that runtime itself before acknowledging cleanup. Forced guardian cleanup is recorded and prevents a passing result when the adapter claimed clean shutdown.

Unregistered detached VMs are outside this ownership guarantee. If the guardian dies, the runner holds the load lock during fallback cleanup; pending spawns or unknown ownership leave a persistent lease that blocks new load.

On Linux, the workload processes are pinned to one allowed logical CPU. On macOS, the result explicitly says that affinity was not enforced. The child environment contains only an allowlist plus a fixed hash seed and single-thread math and OTP settings. Environment values and cookies are not logged. Adapters must not write cookies, secret arguments, or customer data into artifacts. Use synthetic payloads only.

The guardian samples process count, thread count, RSS, elapsed time, and artifact size. It uses psutil for process creation times, descendants, and per-process RSS because the standard library's resource counters do not provide that portable process identity and tree view. These are watchdog budgets, with possible overshoot between samples. They are not allocation limits. There is no claim that macOS `RLIMIT_RSS` or `RLIMIT_AS` caps physical memory. Core dumps are disabled. An inherited per-process CPU limit is separate from the elapsed-time and observed process-tree budgets.

All timed workstreams must honor `/tmp/pybeam-benchmark-load-<uid>.lock` with an exclusive advisory `flock`. This harness fails rather than waiting if another owner holds it. This cannot stop other tools or builds that ignore the lock. Every run in this stage is labelled `uncontrolled_smoke`, including runs while the other workflow builds. Do not publish comparative performance claims from these runs.

## Artifacts and statistics

A run directory contains `provenance.json`, `trials.jsonl`, and `summary.json`. Each `trial-N/` contains the following:

* `config.json` preserves the full request and its bounds.
* `adapter.json` preserves completion evidence and phase durations, or the unsupported outcome.
* `receipts.jsonl` contains one validated raw receipt per measured message on passing trials.
* `resources.jsonl` contains individual PID and creation-time identities with sampled RSS, CPU seconds, and thread counts. Harness and guardian observations are separate from adapter and descendant observations.
* `outcome.json` records exit status, enforcement, cleanup duration, forced cleanup count, and survivors.
* `stdout.log`, `stderr.log`, and `guardian.log` retain diagnostic output. Broker-owned processes have numbered output logs. Failure trials retain whatever evidence was produced; they do not receive fabricated completions.

Provenance includes both checkout SHAs, dirty flags, diff hashes, and a content fingerprint of tracked and nonignored source files. It also includes the harness executable hash, dependency versions, host details, and available OTP and Elixir executable locations. Unused runtimes are not started for version probes. Actual OTP, ERTS, and Elixir versions belong in the result from the runtime used by an adapter. Native build assets ignored by Git require the adapter's loaded asset hashes and build manifest described above.

Latency p50, p95, and p99 use nearest rank over actual acknowledgment durations. Throughput uses completed events divided by the measured interval. Repeat statistics use fresh subprocess runs and sample standard deviation for throughput, latency quantiles, and each phase duration. One observation has zero sample variability by convention, not evidence of stability.

RSS is sampled resident memory, not PSS. Shared pages may be counted more than once when summing processes. Separate adapter and descendant peaks need not occur together, so the simultaneous tree peak is reported separately. Very short processes can fall between samples. CPU values are the largest observed lifetime user plus system seconds per identity, including startup and warmup. They are lower bounds, not isolated measurement-phase CPU usage. Do not claim complete peak memory or exact phase CPU accounting from these observations.

## Next independent work

1. Run the broader idiomatic Python reference and a native Erlang or Elixir reference against this scalar sequence. Keep Python `TaskGroup`, bounded `Queue`, and acknowledgment behavior. Return unsupported rather than weakening a requested guarantee.
2. Adapt actual PyBEAM native actors and messages only after validating ownership and recording the loaded native build.

Reuse `pybeam.actors.protocol`, explicit `UnboundedMailbox`, and existing native fixtures. Do not use the prototype decorator or direct calls as native actor evidence.
3. Coordinate additions to the contract before implementing idle and active rooms with churn, 10 percent overload isolation, handler failure and supervised reset, slow consumers and cancellation, and structured or large binary individual and batch bridge work.

Specify terminal state and delivery guarantees for each. Keep unsupported measurements null.
4. Validate execution modes independently. Treat two-node operation as a separate feasibility check. Add bounded long runs only after small budgeted checks. No entity count of 10,000 or above is a default.
### Use one clock domain



Consume and verify the existing workflow's GenStage, streaming, Pydantic AI, Discord, and integration results instead of implementing those pipelines again. The parent can later schedule controlled serial comparisons and write a comparative report from actual matching results.

## Appendix: environment and provenance

This directory is an experiment. It does not change PyBEAM APIs or the MVP gate. The first adapter is a small correctness smoke check, not a full Python reference implementation. Workflow `wf_7847dc24a4af` owns the application and integration workload prototypes. Do not copy those implementations here.

The original contract used conventional Python 3.14 and a separate environment without installing the root package or building native assets; the current editable dependency is described below. The original report did not establish a public native source installation. The legacy reference reuses the read-only private assets in `../native-bench-pybeam`. Its adapter verifies the actual manifest and files before and after each trial. Keep compilation parallelism at most two. Do not fix native runtime failures merely to make a benchmark pass.

The managed PyBEAM launcher currently starts its own VM session internally. This contract does not claim that a polling watchdog can guarantee discovery if such an unregistered VM outlives a host that dies immediately. An adapter unable to use guardian-owned creation must return unsupported for that launch path.

Do not alter the production launcher just for this benchmark. If the guardian dies, the runner retains the load lock during fallback cleanup of registered PID and creation-time identities. A pending spawn or unknown ownership leaves a persistent lease that blocks new load. This does not prove cleanup of unregistered escapes.

Machine failure and deliberately escaping ownership remain outside this guarantee. Independent review accepted this guardian failure fix. That acceptance does not approve benchmark reference correctness. Native crash probes require `unsafe_native_crash=true`, an explicit adapter, and guardian-owned subprocesses.

They are never part of the default smoke run.

### Current build boundary

`python -m benchmarks.canonical_build` runs `tools/build_native.py` and records the rebar-fetched erlang-python application in the existing benchmark manifest format. Current runs require the clean revision in `rebar.lock`, source fingerprints, matching Python/CMake hashes and native asset hashes. Archived records retain their original provenance and are not rewritten.

Rebar leaves `ebin/erlang_python.app` untracked. Only that declared, hashed generated file is excluded from the native source fingerprint; tracked files and other nonignored untracked files remain checked. No Git ignore rules are changed. Historical manifests can be checked against their explicitly recorded revision with `verify(..., expected_revision=manifest.native.sha)`; current launches always use the lockfile revision.

### Recorded commands

```sh
uv run --project benchmarks python -m benchmarks run
uv run --project benchmarks python -m pytest -c benchmarks/pyproject.toml benchmarks/tests
(cd benchmarks && uv run ruff check . && uv run ruff format --check .)
uv run --project benchmarks ty check --project benchmarks --extra-search-path . --extra-search-path src --extra-search-path typings
uv run --project benchmarks python -m benchmarks schema
```

### Public reproduction

From a fresh Git checkout, install [mise](https://mise.jdx.dev/getting-started.html) and a C/C++ compiler with make, then run these commands from the repository root. Do not run `mise run setup` or add sibling checkout overrides. The native build uses at most two compiler jobs; dependency installation alone does not build it.

```sh
mise trust
mise install
mise exec -- uv sync --locked --project benchmarks
mise exec -- uv run --locked --project benchmarks python -m benchmarks.canonical_build
mise exec -- uv run --locked --project benchmarks python -m benchmarks.native_reference --build
mise exec -- uv run --locked --project benchmarks python -m benchmarks run --config benchmarks/examples/serial_pybeam.json
mise exec -- uv run --locked --project benchmarks python -m benchmarks run --config benchmarks/examples/serial_python.json
mise exec -- uv run --locked --project benchmarks python -m benchmarks run --config benchmarks/examples/serial_erlang.json
```

These short serial checks run 100 events per trial, three trials per backend. They create new ignored result directories; they do not regenerate identical historical values or overwrite archived evidence. `--native-source` still selects an explicit matching native checkout; the showcase requires `--trusted-native-root _build/hornbeam/lib/erlang_python`.
