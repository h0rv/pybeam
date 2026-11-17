# Execution modes: measured development builds

These are single trials on one Linux x86-64 VM with eight CPUs, not compatibility certification or production limits. Conventional CPython is the default. See the [execution model](execution-model.md) for ownership, cancellation and durability boundaries.

## Mapping work to execution resources

### Worker: conventional Python

![Worker on CPython 3.14: BEAM callers share a bounded context pool. Context threads share the main interpreter and GIL inside the BEAM OS process. A native abort kills the whole VM.](assets/worker-314.svg)

### Worker: free-threaded Python

![Worker on CPython 3.14t: context threads share the main interpreter without a GIL in the tested build. Threads can run Python in parallel, but a native abort still kills the whole VM.](assets/worker-314t.svg)

### Own-GIL subinterpreters

![Each owngil context has a thread and separate subinterpreter state, with its own GIL on 3.14 and no GIL in the tested 3.14t build. All share one BEAM OS process, so a native abort kills the whole VM.](assets/owngil.svg)

### Isolated children

![Each isolated context owns a separate child OS process with a Python interpreter and execution thread. A native abort kills only that child; the BEAM VM survives and the child restarts.](assets/isolated.svg)

The isolated diagram describes the fixed runtime measured below; it requires the PR #5 bootstrap fix.

A million BEAM processes do not imply a million interpreters. The [generic router][router] reuses a bounded context pool. Each embedded context currently costs one pthread; isolated contexts instead cost a child process. This is **not actor pooling**: today's actors/tasks allocate worker contexts per native child, so a million Python actors would still imply roughly a million context pthreads plus other overhead.

## Raw context matrix

CPython **3.14.10 / 3.14.10t**; `erl +S 8:8`. Creation is the cumulative mean at **256 contexts**. RSS is the baseline-subtracted VM plus child RSS, divided by context count; it is summed RSS, not PSS. Calls are 10,000 sequential `operator.add(1, 2)` calls after warm-up. CPU work is a calibrated ~200 ms pure-Python sum of squares. Speedup is `N × wall1 / wallN`; both scaling columns below use **N=8**. No affinity pinning or repeated-trial confidence intervals.

| Mode / build | Works? / capacity reached | Create ms | RSS/context KiB | Call p50/p99 µs | CPU across 8 contexts | 8 threads in one context | Module state | pydantic / numpy | `os.abort()` |
|---|---|---:|---:|---:|---:|---:|---|---|---|
| worker / 3.14 | Yes; 1000 | 1.335 | 15.05 | 17 / 31 | 0.96× | 0.97× | Shared | Both import | VM dies, exit 134 |
| worker / 3.14t | Yes; 1000 | 1.536 | 21.22 | 29 / 51 | 4.71× | 5.03× | Shared | Both import | VM dies, exit 134 |
| owngil / 3.14 | Yes; 1000 | 78.560 | 10,800.31 | 14 / 23 | 5.27× | 0.96× | Separate | Both rejected | VM dies, exit 134 |
| owngil / 3.14t | Yes; 288 in 100 s | 345.199 | 28,525.98 | 14 / 21 | 4.14× | 4.28× | Separate | Both rejected | VM dies, exit 134 |
| isolated / 3.14 | With PR #5 fix; 1000 | 90.065 | 24,402.20 | 76 / 146 | 4.73× | 1.23× | Separate | Both import | VM survives; child restarts |
| isolated / 3.14t | With PR #5 fix; 314 in 100 s | 320.468 | 40,167.00 | 81 / 138 | 4.51× | 7.44× | Separate | Both import | VM survives; child restarts |

Creation checkpoints were 1, 8, 64, 256 and 1000, with a 100 s deadline. The two time-capped runs did not reach the 1000 checkpoint; neither reached an allocation limit. Reaching 1000 is a requested ceiling, not a discovered maximum. A Python `ValueError` returned an error in every mode/build and the next call returned `{ok,3}`. In isolated mode the abort caller received `{error,{child_exited,{signal,6}}}`; the context survived and both same-context and fresh-context calls returned `{ok,3}`.

`sys._is_gil_enabled()` stayed false in all tested 3.14t contexts, including after successful pydantic and numpy imports. Both imports failed in owngil without enabling the GIL. Conventional contexts reported true. Import versions: pydantic **2.14.3**, pydantic-core **2.50.4**, numpy **2.5.5**, installed from wheels into separate environments. CPU probes preceded optional-library imports. Ratios near one, including conventional isolated threads at 1.23×, do not establish parallel bytecode execution.

## PyBeam task probe

The public `TaskSupervisor` / `TaskFunction` probe ran identical work in N tasks, after importing PyBeam and pydantic. Creation was outside timing. Timing began at a shared start gate and ended when every `TaskRef.result()` completed. The launcher retained `+S 1:1`.

| Build / task loop | Wall ms, N=1 / 2 / 4 / 8 | Speedup, N=1 / 2 / 4 / 8 | GIL inside tasks |
|---|---|---|---|
| 3.14 / shared (default) | 200 / 395 / 768 / 1502 | 1.00 / 1.01 / 1.04 / 1.07× | Enabled |
| 3.14t / shared (default) | 229 / 430 / 878 / 1719 | 1.00 / 1.07 / 1.04 / 1.07× | Disabled |
| 3.14 / `dedicated_loop=True` | 1187 / 1368 / 1759 / 2525 | 1.00 / 1.74 / 2.70 / 3.76× | Enabled |
| 3.14t / `dedicated_loop=True` | 1223 / 1244 / 1231 / 1242 | 1.00 / 1.97 / 3.97 / 7.88× | Disabled |

Dedicated tasks had **~1 s extra completion latency per batch on both builds; its source is untraced**. This inflates the conventional end-to-end ratio: 3.76× is not CPU parallelism. CPU span, measured from the first task's CPU start to the last task's CPU end, was **178 → 1517 ms** at N=1 → 8 on 3.14 (**0.94×** throughput), versus **215 → 232 ms** on 3.14t (**7.43×**). Dedicated tasks used eight distinct threads at N=8; shared-loop tasks used one thread and did not parallelize on either build.

## Constraints and evidence

- **owngil extensions:** [the interpreter configuration][own-gil] sets `check_multi_interp_extensions = 1`. Actual imports returned `module pydantic_core._pydantic_core does not support loading in subinterpreters` and `module numpy._core._multiarray_umath does not support loading in subinterpreters` (the latter wrapped by NumPy). Free-threading does not remove subinterpreter compatibility requirements.
- **Actors require the main interpreter:** [local-object retention/unwrapping][local-object] rejects other interpreters with `Actors require the main interpreter`; [context validation][local-context] rejects subinterpreters with `local_objects_require_worker`.
- **Mode is not a PyBeam actor/task option:** [py_data_actor.erl][data-actor], [py_object_actor.erl][object-actor] and [py_task.erl][task] hard-code `mode => worker`. `dedicated_loop=True` selects a loop, not a different interpreter or process.
- **Isolated bootstrap:** [package initialization][bootstrap-import] eagerly imported [_supervision, which imports erlang][bootstrap-supervision], before [the child installed its synthetic erlang module][bootstrap-child]. This produced `ModuleNotFoundError: No module named 'erlang'` and `{handshake_failed,closed}`. [PR #5](https://github.com/h0rv/erlang-python/pull/5) fixes the import order. The isolated results above used its `249a970` fix; fork main at `54ccee8` still lacks it. PyBeam follows main, not that PR commit.
- **owngil is not a crash boundary:** its subinterpreter runs inside the BEAM address space. The separate crash probes terminated the whole VM on both builds. Only isolated contained `os.abort()` in these trials.

## Decisions and open questions

- Default: conventional **3.14 worker**, because it is reproducible and the packaged wheels are conventional.
- Recommended development path for CPU parallelism: **3.14t with dedicated-loop tasks**, opt in with `PYBEAM_ALLOW_FREE_THREADED=1`. Free-threaded packaged wheels have not been built. Shared-loop tasks do not parallelize on either build.
- Restrict owngil experiments to extension-free code; never select it for PyBeam actors today.
- Use isolated for crash containment once the fork fix lands. It is not currently a PyBeam actor/task backend option.
- A per-actor/task mode option first needs erlang-python to accept a mode rather than hard-coding worker. Mode acceptance alone does not solve main-interpreter object references or process serialization. This is upstream/fork work, not a PyBeam scheduler or fallback.
- Open: complete 3.14t integration acceptance, including the hang and `ws_session:send_sync/3` `undef`; the 3.14t owngil/isolated creation slowdown; the untraced dedicated-loop latency.

The prototype passed lint/format/strict typing. Core units: conventional **612 passed, 11 skipped**; 3.14t **608 passed, 4 failed, 11 skipped** (conventional-only wheel-manifest fixtures). Full 3.14t integration was interrupted after 740 s: **29 failed, 26 passed, 5 skipped, 7 errors**, including source-layout/asset setup issues, so this is not a controlled comparison to the earlier two-WebSocket-failure baseline. After private sibling-path setup, focused task/supervision/actor suites passed **32 tests**. An actor argument-retention assertion also failed on conventional Python. None of this establishes full free-threaded runtime compatibility.

## Reproduce

Toolchain: Linux x86-64, eight CPUs, CPython **3.14.10** (conventional and free-threaded), Erlang/OTP **29.1.2**, rebar3 **3.28.0**, CMake **4.5.0**, uv **0.13.8**. PyBeam prototype: `98a72e2`; raw free-threaded and fixed isolated runtime: erlang-python `249a97016479c44ef5cf4a51ebe70a06176e32ec`. Earlier conventional worker/owngil measurements reused the existing PyBeam native build; its revision was not recorded in the original measurement records. Later combined records label the revision used for the extra probes. Do not treat the rows as a controlled same-revision experiment. Subsequent PyBeam rebuild against fork main `54ccee8367341eb83acab1280b4316ba034e3bf4` also detected free-threaded Python.

Use a private PyBeam worktree with private sibling runtime sources. Link `_checkouts/erlang_python`, `hornbeam`, `ws`, `h1` to those sources, as setup does; do not reuse another checkout's mutable native build. Hornbeam was `d1ccbbe`, ws `4a1d356`, h1 `e1a01f3`. Some tests also assume sibling paths and a `_build/default` profile; the prototype supplied private sibling links and a `default` alias to `hornbeam`. Root-run examples need explicit development-runtime configuration; no free-threaded packaged assets were staged.

```bash
# In the prepared private PyBeam worktree, with mise on PATH:
mise trust
mise install
export UV_PYTHON="$(mise exec -- uv python find 3.14t)"
export PYBEAM_ALLOW_FREE_THREADED=1
mise exec -- uv sync --locked --all-extras
mise run build
mise run check
mise run test
mise run integration
mise exec -- uv run --locked --all-extras pytest \
  tests/test_tasks_native.py tests/supervision/test_runtime.py \
  tests/test_actors_native.py --run-runtime -q
```

For conventional builds, select `UV_PYTHON` with `mise exec -- uv python find 3.14.10`, unset `PYBEAM_ALLOW_FREE_THREADED`, and use a separate environment and native build tree. `pyproject.toml`, `uv.lock` and the default mise Python pin were unchanged.

The external raw harness is an Erlang module (`modes_bench`) plus a Python helper (`mode_helper`). It creates contexts directly with `py_context:new(#{mode => M})`, calibrates `sum(i*i for i in range(K))`, checks a module global, raises `ValueError`, and calls `os.abort()` in a separate VM. An extra probe starts 1/2/4/8 `threading.Thread`s inside one context, then checks live GIL state before/after pydantic and numpy imports. Its runner unsets `PYTHONHOME`, `PYTHONPATH`, `PYTHON_GIL`, disables core dumps/bytecode writes, uses separate measurement/capacity/crash/extra VMs with a 125 s external timeout, and kills remaining process-group members. It records JSON, errors and VM exit status.

With those external files restored, `HARNESS` pointing to their directory, `BUILD` to the chosen rebar profile, and `PACKAGE_PATH` to that build's dependency site-packages, the VM invocation is:

```bash
eval "$(mise env -s bash)"
unset PYTHONHOME PYTHONPATH PYTHON_GIL
export PYTHONDONTWRITEBYTECODE=1 PACKAGE_PATH
ulimit -c 0
erlc -o "$HARNESS" "$HARNESS/modes_bench.erl"
# Repeat for worker/owngil/isolated and measure/capacity/crash/extra.
export MODE=worker PROBE=measure RESULT_FILE="$HARNESS/worker-measure.json"
setsid timeout --foreground -k 3 125 erl +S 8:8 +Bi -noshell -noinput \
  -pa "$HARNESS" "$BUILD"/lib/*/ebin "$BUILD"/checkouts/*/ebin \
  -s modes_bench main &
pid=$!
status=0
wait "$pid" || status=$?
kill -KILL -- "-$pid" 2>/dev/null || true
printf 'VM exit: %s\n' "$status"
```

The separate raw 3.14t tree was configured explicitly before `rebar3 compile`:

```bash
cmake -S c_src -B _build/cmake \
  -DPython3_EXECUTABLE="$UV_PYTHON" \
  -DPython3_ROOT_DIR="$("$UV_PYTHON" -c 'import sys; print(sys.base_prefix)')" \
  -DPython3_FIND_ABI='ANY;ANY;ANY;ON'
CMAKE_BUILD_PARALLEL_LEVEL=2 rebar3 compile
```

The external PyBeam script uses only public task APIs, a bounded `TaskSupervisor`, `TaskFunction(..., dedicated_loop=...)`, a cross-thread start gate, and per-task CPU timestamps/thread IDs/GIL status. Run the same script with each matching Python environment and a working-directory configuration containing `[tool.pybeam] development_runtime = "/absolute/path/to/profile"`. It calls `pybeam.run(main)`; no custom Erlang launcher or scheduler change is needed.

With `PYBEAM` naming the SDK source worktree, `PYBEAM_ENV` its matching dependency environment, and `PROBE_PROJECT` the configured working directory, the command is:

```bash
cd "$PROBE_PROJECT"
PYTHONPATH="$PYBEAM/src:$PYBEAM" PROBE_OUTPUT="$RESULTS" \
  "$PYBEAM_ENV/bin/python" "$HARNESS/pybeam_tasks_probe.py"
```

A reviewed, parameterized version of these harnesses belongs in `benchmarks/` before they become a repeatable gate. They are not included here; this document and commands alone cannot reconstruct the missing external source files.

[router]: https://github.com/h0rv/erlang-python/blob/54ccee8367341eb83acab1280b4316ba034e3bf4/src/py_context_router.erl#L524-L531
[own-gil]: https://github.com/h0rv/erlang-python/blob/54ccee8367341eb83acab1280b4316ba034e3bf4/c_src/py_nif.c#L3044-L3062
[local-object]: https://github.com/h0rv/erlang-python/blob/54ccee8367341eb83acab1280b4316ba034e3bf4/c_src/py_local_object.c#L32-L57
[local-context]: https://github.com/h0rv/erlang-python/blob/54ccee8367341eb83acab1280b4316ba034e3bf4/c_src/py_local_object.c#L377-L382
[data-actor]: https://github.com/h0rv/erlang-python/blob/54ccee8367341eb83acab1280b4316ba034e3bf4/src/py_data_actor.erl#L8-L20
[object-actor]: https://github.com/h0rv/erlang-python/blob/54ccee8367341eb83acab1280b4316ba034e3bf4/src/py_object_actor.erl#L40-L44
[task]: https://github.com/h0rv/erlang-python/blob/54ccee8367341eb83acab1280b4316ba034e3bf4/src/py_task.erl#L8-L42
[bootstrap-import]: https://github.com/h0rv/erlang-python/blob/54ccee8367341eb83acab1280b4316ba034e3bf4/priv/_erlang_impl/__init__.py#L69
[bootstrap-supervision]: https://github.com/h0rv/erlang-python/blob/54ccee8367341eb83acab1280b4316ba034e3bf4/priv/_erlang_impl/_supervision.py#L4
[bootstrap-child]: https://github.com/h0rv/erlang-python/blob/54ccee8367341eb83acab1280b4316ba034e3bf4/priv/py_isolated_child.py#L185-L190
