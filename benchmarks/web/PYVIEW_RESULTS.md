# PyView background-push load

From the repository root, after the setup in the appendix:

```sh
mise exec oha@1.17.0 -- taskset -c 4-7 uv run --no-sync python -m benchmarks.web.pyview_load --output "$HOME/pyview-load-new"
mise exec -- python -m benchmarks.web.pyview_charts "$HOME/pyview-load-new"
```

Add `--smoke` to the first command for a two-second protocol check with zero
and four views (not a capacity measurement). Output directories must be new
and outside the repository. The second command regenerates the SVGs using
only Python's standard library; it expects full 30-second measurements.

## Results

**Hornbeam failed during connection setup in two of three repeats.** Its first
repeat stopped at 250 views, its second completed through 2000, and its third
stopped at 500. Both failures logged `ets:new(hornbeam_ws_sessions, ...)` with
`cause => already_exists`; the client received a connection reset waiting for
a join reply. `hornbeam_ws_handler:init/2` calls `register_session/2`, whose
`ensure_session_table/0` checks then creates a named ETS table without handling
concurrent creation. Server stack traces identify that path in both failures;
this was not five-item WebSocket protocol interception. No runtime fix or
serial-join workaround was applied. Higher counts were cut for those repeats.

Uvicorn completed all counts in all three repeats. Charts show only completed
trials: dots are medians, whiskers are min–max, and **n** is successful repeat
count. Hornbeam's 500–2000-view values each have **n=1**, not three-repeat estimates.
At 2000 views incremental memory was 86.794 kB/view for Hornbeam (n=1)
and 43.385 (43.348–43.411) kB/view for Uvicorn (n=3).

![Container memory by view count](pyview-memory.svg)
![Incremental memory slope relative to zero views](pyview-per-view.svg)

![Ping p50 latency](pyview-ping-p50.svg)
![Ping p99 latency](pyview-ping-p99.svg)

![LiveView event p50 latency](pyview-event-p50.svg)
![LiveView event p99 latency](pyview-event-p99.svg)

Every completed HTTP trial returned 3000 HTTP 200 responses without oha errors.
Every completed nonempty trial received all 300 event replies. Aggregate tick
delivery was 99.858–100.003% of views × elapsed seconds; timer phase and window
boundaries allow counts slightly above 100%.

![Ticks received versus expected](pyview-ticks.svg)

## What this measures

The same self-contained PyView app runs on both servers: each view schedules
`schedule_info` at 1 Hz, increments a counter, and re-renders. `GET /ping` returns
`ok`. There are no PyBEAM services. The Python client fetches a fresh page per
view for its signed session and CSRF token, validates the Phoenix WebSocket join,
and continuously reads nonempty diffs. Joins run in batches of 32 across four
loopback source addresses.

Each count gets a fresh container, five seconds to settle after joining,
then one cgroup v2 `memory.current` sample. This includes the whole container,
not just Python heap or RSS. The per-view slope is
`(memory_at_N - memory_at_0) / N`, pairing baselines within each repeat.
MB and kB are decimal units. During the next 30 seconds, oha sends 100 requests/s
with concurrency 16 and latency correction. Ten sockets receive one event probe
per second; latency runs from send to successful rendered reply. Each trial's
percentiles are summarized separately, not pooled across repeats.

This is not a saturation test, CPU-heavy workload, browser rendering test,
per-view tick-delay guarantee, or production capacity claim. Slow-consumer
queues, shared state, durability, multi-worker Uvicorn, distributed execution,
and long-term leaks were not measured. Some Uvicorn teardown logs report failed
timer removal after socket closure; disconnect cleanup was not benchmarked.

## Appendix: setup and environment

```sh
mise install
mise exec -- uv sync --locked --all-extras
mise exec -- uv run --locked python tools/build_native.py
mise install oha@1.17.0
docker build -t pybeam-web:jammy benchmarks/web
```

Use the pins in `rebar.config` and `rebar.lock`; no `_checkouts` overrides were
used. Builds need a C compiler and mise tools. The image mounts home read/write
at its original path: use only a trusted image. This is contributor setup,
not an end-user installation claim. The runner holds `measurement_lock`; do not
run benchmark tests or other loads concurrently. Raw JSON, logs, exit states,
and failure tracebacks stay outside Git.

- Measured 2026-11-18; raw run: `$HOME/pyview-measured`. An earlier partial run
  overlapped tests and was discarded; the recorded run had no concurrent tests.
- Host: Intel Xeon x86_64, 8 vCPUs, 31 GiB RAM, no swap; Ubuntu 22.04.5,
  Linux 6.8.0-1060-aws; Docker 29.9.2, cgroups v2, oha 1.17.0.
- Both containers: CPUs 0–1, two-CPU quota, 2 GiB memory, no additional swap,
  host network, 200,000 file descriptors. Clients/oha: CPUs 4–7, outside cgroup.
- Hornbeam: two worker contexts, one shared loop, two acceptors, two BEAM
  schedulers. Uvicorn: one worker, asyncio, h11, websockets. Compression and
  client automatic pings disabled. This reuses the web benchmark configuration,
  not a matched Python worker-count comparison or tuning search.
- Python: conventional CPython 3.14.10 (GIL enabled); OTP 29.1.2 / ERTS 17.1.1.
- PyView 0.9.0 at `d29dda1fb1f3a09c3c0a3c9102a8f0490f6f9972`;
  Starlette 1.10.0, Uvicorn 0.54.0, websockets 16.1.1.
- PyBEAM base: `bd1055701c97ec42d2075814781a37dba4a2c863`, with benchmark
  additions committed alongside this document.
- Hornbeam: `a04541d1d5667640ed59fafc1b5b60a5575b38c0`;
  erlang-python: `40e5c41e898f77c424ac54a038305fc90bb1d7d8`;
  erlang-ws: `09bea3e650187aaeeaba1a8c734338bdeedfe136`;
  erlang-h1: `40889c592e5c7ef9e1cb8e6e5560d96d6cb5fa7a`.
- Container base: Ubuntu 22.04, glibc 2.35, digest pinned in `Dockerfile`.
