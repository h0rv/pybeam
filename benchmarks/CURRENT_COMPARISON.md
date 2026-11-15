# Current runtime scalar references

This is a private integration candidate. It needs a fresh independent review because the runtime changed. It is not release clearance or a performance ranking.

## Sources and build

The candidate started at PyBEAM `421415a4dfa7d4758b26a0144441f9eaf8290551`. The approved reference changes were selected from `2c7b846`. No old SDK core files were merged. The measured code is `fe71ba1f52d239871a97cb2e4b87bcdf2b6794aa`.

The private native checkout is `../native-benchmark-current` at `a1ca04bfcaea817654ec7d1972f210c7a60075c4`. Its source is unchanged. Its NIF and Erlang modules were built in that private checkout with the recorded builder commands. CMake used at most two jobs. These are not copied canonical artifacts. No canonical native build was performed.

The loaded private NIF SHA256 is `8a98acf2a0cdd74f03abf8c9abd53255ddeaf171734f87d1d5cdfdf5681c3b8f`. The build uses CPython 3.14.10 and OTP 29.1.2 with ERTS 17.1.1. The host is macOS 27.2.1 arm64. `otool` reports a minimum macOS version of 27.2 for this artifact. No older OS compatibility was tested. Each native trial contains its build manifest, asset hashes and source fingerprint.

The corrected historical reports and measurements from SDK `6bd1069` with native `9baab2f` are unchanged. They are a separate baseline. They were not relabeled as current results. Git comparisons against `2c7b846` found no changes in the corrected, serial or archived evidence paths.

## Supported work

All three profiles run only uniform `rooms.local` with scalar integer 1. Each trial has two entities, 100 measured events, 17 warmup events and a window of four requests. The destination queue capacity is four for Python. The native mailboxes remain unbounded. The common scope is client admission only, not a destination mailbox bound.

The raw adapter imports `ActorSpec` and `ActorRef` from `pybeam.actors.protocol`. It imports `OwnedActorRef` and `spawn` from `pybeam.actors.native`. It uses explicit `UnboundedMailbox`. It is the raw unbounded API on the current runtime, not the new `@actor` safe data default. No public compatibility aliases, stubs or runtime relaxations were added.

The guardian owns VM startup before embedded work begins. The existing `spawn_owned`, `py_launch` and runtime loader remain in use. No new supervisor or public launch path was added. The trusted benchmark bootstrap selects the private runtime. A core runtime asset override was not needed.

Handler replies contain the received message ID and the handler's own bound room identity after the effect. Receipts use those observed fields. Message models are prepared before timing. Result models are constructed after timing. Actual API calls and identity checks remain inside timing.

Every healthy raw trial reports one interpreter with the GIL enabled. Native context inventory goes from one context to three and back to one. The loaded C context counters report created/destroyed pairs of 1/0, 3/0 and 3/2. Actor PIDs disappear and the Python state is collected before the VM completes cooperative shutdown. None of these contexts are described as CPU cores.

Churn and higher workload families remain unsupported here. No HTTP comparison, new actor frontend measurement, durability or automatic retry is claimed.

## New recorded results

`results/current-fe71ba1/` contains nine passing trials. Backend order rotates as Python/Erlang/PyBEAM, Erlang/PyBEAM/Python and PyBEAM/Python/Erlang. Every backend was run once in each position. Each result has 100 receipts and final counts of 50/50. All cleanup states are confirmed with no survivors or forced cleanup.

`results/current-negative-fe71ba1/` contains 16 rejected trials. Ten retain the approved identity and count controls. Six additional controls let all 100 effects finish before the common guardian rejects a wrong route, all IDs equal to zero or IDs increased by 100. Those six results still have counts of 50/50. Rejected trials have null completion, latency and throughput summaries. Cleanup is confirmed without forced cleanup.

The global nonblocking flock was used for every trial. No cases ran in parallel and no blocked trial was automatically retried. Other host activity was not controlled, so all results remain `uncontrolled_smoke`. RSS is a sampled lower bound, not PSS. Existing per-PID CPU accounting is unchanged.

`results/current-fe71ba1/artifact-manifest.json` records stable file sizes and SHA256 values for 427 new evidence files. The recorded native asset hashes were checked again against file bytes. Python and pure OTP provenance each contain only the Python source checkout. Neither claims an unused PyBEAM native source.

## Checks

The final check logs are under `results/current-fe71ba1/checks/`.

* Ruff lint and format checks passed for the checkout.
* Strict ty passed for the root and the separate benchmark project, with warnings treated as errors.
* All 132 benchmark tests passed without an inherited PYTHONPATH. This includes the original 121 tests, five current profile checks and six full delivery controls. The current native fixture executed loaded C counters.
* The nonruntime, nonbrowser core selection passed 433 tests, with 58 deselected. It included optional import isolation, style and public typing tests. A private CPython 3.14.10 test environment explicitly installed the optional framework dependencies and Hornbeam's Python package. Earlier attempts using a temporary uv overlay could not find the sibling ty executable. Those setup failures were not counted as passes. The separate test environment resolved that issue without source changes.
* The actual Chrome test passed with `--run-browser`. It started Python and Erlang reference profiles, checked their displayed results and SVG values, tested cancellation and confirmed cleanup after guardian death and application shutdown. No page or console errors were reported. It is now marked `browser` and skips unless explicitly enabled. A failed job reports its status and logs instead of only waiting for a completion label.

The existing five-second passive peer close flake was not changed or retested as part of these checks. No full native CT suite or installed-wheel gate was run. The NIF source is unchanged.

## Local use

Run these from this checkout. Build only your private native checkout.

```sh
uv sync --extra showcase
uv sync --project benchmarks
uv run --project benchmarks python -m benchmarks.pybeam_reference_build \
  ../native-benchmark-current --python /opt/homebrew/opt/python@3.14/bin/python3.14
uv run --project benchmarks python -m benchmarks.native_reference --build
uv run --project benchmarks python -m benchmarks run --config benchmarks/examples/serial_python.json
uv run --project benchmarks python -m benchmarks run --config benchmarks/examples/serial_erlang.json
uv run --project benchmarks python -m benchmarks run --config benchmarks/examples/serial_pybeam.json \
  --native-source ../native-benchmark-current
uv run --extra showcase python -m examples.showcase \
  --trusted-native-root ../native-benchmark-current
```

Open `http://127.0.0.1:38921`. The website uses only fixed commands. “Run all available benchmarks” queues the enabled scalar profiles serially. Missing native assets disable the native card with a reason. The website does not build or install anything. Host and Origin checks remain local only. No public server or authentication claim is made.

Python-only jobs have null native configuration. PyBEAM still requires its actual source and verified NIF assets. Source archives without Git metadata have a clear unsupported capability message rather than an invented Git revision. The separate QA fixes are commits `0f9e85b` and `a7e5b84`. A standalone version is commit `a7ee7ace6e3d78ab6d1e5f30bb851f89c244dd24` on `experiment/benchmark-current-qa`, based directly on `421415a`. It includes the nullable schema and does not depend on the reference backends. Its offline gates passed 11 focused tests, with Chrome correctly skipped without opt-in. No timed load was launched after the requested clock pause.
