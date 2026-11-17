# ASGI serving benchmark

One application returns 19-byte JSON from `GET /` and echoes text on `/ws`.
The four launch paths are A: Hornbeam; B: Uvicorn in PyBeam's managed runtime;
C: standalone Uvicorn, one worker; C2: standalone Uvicorn, two workers.
This measures serving only, not shared state, persistence, or application work.

## Reproduce

Run from the repository root on a Linux host with CPUs 0–7, container-engine
access, and sibling runtime checkouts as described in
[development.md](../../docs/development.md). Use the source revisions below.
Create the two `_checkouts` links once, if they do not already exist.

```sh
mise install
mise exec -- uv sync --locked --all-extras
mkdir -p _checkouts
ln -s ../../hornbeam _checkouts/hornbeam
ln -s ../../erlang-python _checkouts/erlang_python
mise exec -- uv run --locked python tools/build_native.py
mise install oha@1.17.0
mise use --global oha@1.17.0
docker build -t pybeam-web:jammy benchmarks/web
mise exec -- uv run --no-sync python -m benchmarks.web.run --smoke --output "$HOME/web-smoke"
mise exec -- uv run --no-sync python -m benchmarks.web.run --output "$HOME/web-full"
```

Output directories must not exist and must be outside the repository. The runner
stores raw measurements and container logs there, then stops/removes each
container. The image mounts the current user's home directory read/write at the
same path so the built runtime and virtual environment remain usable; run only
with a trusted image. No raw results or logs are committed.

## Method

Measured 2026-11-17 on an otherwise idle eight-vCPU virtual machine. All table
cells with parentheses are **median (minimum–maximum)** over three repeats,
not confidence intervals. Latencies are milliseconds. A fresh container is used
for each saturation pair, fixed-rate run, and WebSocket ramp in each repeat.

| Component | Configuration |
| --- | --- |
| Host | x86_64, Intel Xeon, 8 vCPUs, 31.3 GiB RAM, no swap |
| OS | Ubuntu 22.04.5, Linux 6.8.0-1060-aws, glibc 2.35 |
| Python | CPython 3.14.10, conventional build, live GIL enabled |
| BEAM | OTP 29 / ERTS 17.1.1 |
| Python libraries | FastAPI 0.163.0, Uvicorn 0.54.0, websockets 16.1.1 |
| Load / container tools | oha 1.17.0, Docker 29.9.2, cgroups v2 |
| PyBeam base | `68e7b225e48adc5008ec8cb15f7f48842628ed3a` |
| Hornbeam | `c97d49e6537603ec46cd55116a1f1bb1e49a69ee` |
| erlang-python | `56d20d46fc592a331e579bab7eddc3be75d19d56` |
| Container base | Ubuntu 22.04, glibc 2.35; digest pinned in `Dockerfile` |

Every server uses host networking and exactly
`--cpus=2 --cpuset-cpus=0-1 --memory=2g --memory-swap=2g`, with
`--ulimit nofile=200000:200000`. The HTTP load generator uses four threads on
CPUs 4–7. Four WebSocket client processes each use one of those CPUs and a
distinct source address, `127.0.0.2`–`127.0.0.5`; no source exceeds 12,500 sockets.
The host's ephemeral-port range is 32768–60999.

Hornbeam uses two schedulers (`+S 2:2`), two worker contexts, two acceptors,
one event-loop pool entry, ASGI lifespan enabled, 30-second request timeout,
and a 3,600,000 ms WebSocket timeout. Managed Uvicorn uses the existing managed
launcher defaults: one scheduler, one context, one event-loop pool entry,
and `ErlangEventLoop`. It does not start Hornbeam's HTTP listener.
Standalone Uvicorn uses `asyncio`; all Uvicorn variants use `h11`, the
`websockets` protocol implementation, backlog 4096, disabled access logs,
disabled WebSocket compression and pings, and a three-second graceful-shutdown
timeout. The optional `httptools` and `uvloop` libraries were not installed.

HTTP saturation runs last 30 seconds at each of c64 and c256, following a
two-second c16 warm-up. Requests use HTTP/1.1 keep-alive with compression disabled
and a five-second timeout. Fixed-rate runs last 30 seconds at c64, with
`--latency-correction -q 251`: half the lowest observed saturation rate, rounded
down. These are successful-response latency percentiles emitted by the load
generator; errors are noted separately rather than treated as latency samples.

WebSockets ramp in batches of at most 64 concurrent handshakes per client
process, to 1,000 / 5,000 / 10,000 / 20,000 / 30,000 / 50,000 total connections.
Each reached step is held for at least 30 seconds unless the server exits.
Opening-handshake timeout is five seconds. Each sampling round echoes a 15-byte
message concurrently on up to 100 rotating sockets per client process, with a
three-second echo timeout and about one second between rounds. The ramp stops
at its first failed step; it does not retry failed opens or refine the limit.
Echo p99 uses `benchmarks.metrics.percentile` (nearest rank) on successful
round trips; disconnected sockets remain counted as errors, not replaced.

Memory is the container's `memory.current`, sampled before opening connections
and after ramping/during each hold. Delta per connection is
`(maximum observed hold memory − idle baseline) / minimum live connections`.
It includes runtime warm-up, allocator retention and kernel socket charges,
not just connection objects. Memory while ramping is not sampled, so failed
ramps do not establish peak memory or a per-connection memory estimate.

## HTTP saturation

| Path | Concurrency | Requests/s | p50 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: |
| A: Hornbeam | 64 | 7,591.73 (7,238.45–7,849.96) | 7.97 (7.77–8.09) | 17.53 (16.32–19.30) |
| A: Hornbeam | 256 | 8,025.10 (7,382.02–8,159.78) | 31.12 (30.78–32.49) | 54.14 (51.98–65.23) |
| B: managed Uvicorn | 64 | 1,444.48 (1,424.85–1,463.51) | 43.01 (42.99–43.03) | 57.26 (53.01–64.00) |
| B: managed Uvicorn | 256 | 754.05 (502.46–2,378.78) | 117.99 (104.98–237.03) | 1,141.98 (142.99–1,228.11) |
| C: Uvicorn ×1 | 64 | 5,839.50 (5,783.07–5,928.25) | 10.68 (10.67–10.95) | 13.10 (11.40–21.74) |
| C: Uvicorn ×1 | 256 | 5,511.02 (5,358.96–5,876.46) | 45.32 (43.18–46.85) | 73.51 (47.18–75.42) |
| C2: Uvicorn ×2 | 64 | 1,554.72 (1,552.75–1,557.39) | 41.00 (41.00–41.00) | 46.07 (43.99–46.94) |
| C2: Uvicorn ×2 | 256 | 6,024.85 (6,021.49–6,097.20) | 41.96 (41.13–41.99) | 51.09 (51.03–54.00) |

B at c256 had 38 request timeouts in the third repeat (99.753% success);
the other saturation runs had no recorded request errors. The large spread is
retained. C2's poor c64 result is repeatable here; its cause was not investigated.

## Fixed-rate HTTP

All twelve runs sustained the offered 251 requests/s without recorded errors.

| Path | Achieved requests/s | p99 ms |
| --- | ---: | ---: |
| A: Hornbeam | 250.99 (250.99–251.00) | 2.67 (2.05–5.54) |
| B: managed Uvicorn | 250.99 (250.99–250.99) | 3.96 (3.68–4.17) |
| C: Uvicorn ×1 | 250.99 (250.98–250.99) | 0.54 (0.44–0.70) |
| C2: Uvicorn ×2 | 250.99 (250.99–250.99) | 1.00 (0.98–1.93) |

## WebSocket capacity

A clean step means every requested connection remained open throughout the
hold, with no opening or sampled-echo errors. Clean counts and first failed
targets were identical across all three repeats. These are coarse measured
bounds, not exact maximum capacities. Remaining connections at a failed step
are not a successful target hold.

| Path | Largest clean step | First failed target | Minimum live at failed step | Idle baseline MiB |
| --- | ---: | ---: | ---: | ---: |
| A: Hornbeam | 5,000 | 10,000 | 8,120 (8,103–8,132) | 90.48 (87.88–90.55) |
| B: managed Uvicorn | 1,000 | 5,000 | 4,095 (4,095–4,095) | 116.18 (116.11–116.55) |
| C: Uvicorn ×1 | 30,000 | 50,000 | 0 (0–0) | 36.51 (36.38–36.54) |
| C2: Uvicorn ×2 | 30,000 | 50,000 | 24,353 (0–24,922) | 97.02 (96.73–97.15) |

- A opened 10,000, then lost connections with close code 1011. Each repeat's
  server log reported `ChannelOverloaded: Native readiness capacity exhausted`.
  Failed-step error counts were 1,933 / 1,872 / 1,962. No container OOM occurred.
- B held 4,095 for the entire failed-step hold, but could not reach 5,000:
  233 handshake timeouts in each repeat. Server logs reported
  `Native FD poll lease limit reached`; the runtime's compiled limit is 4,096.
  No container OOM occurred.
- C and C2 encountered container OOM kills while ramping toward 50,000,
  confirmed by saved container state (`OOMKilled: true`) in all six runs.
  C lost all sockets; C2 sometimes retained a surviving worker's sockets.
  Neither establishes a clean 50,000-connection capacity. No source-port
  exhaustion was observed.
- One early small smoke run also hit Hornbeam's concurrent session-table
  creation error (`ets:new` on an existing named table); the three full repeats
  did not exhibit that error. No runtime workaround was applied.

An interrupted third C ramp was restarted after correcting a container-exit
detection race in the memory sampler. Its complete replacement supplies that
repeat; the interrupted ramp is not pooled with it. C2's third fixed-rate run
and ramp then ran separately with the same settings.

## WebSocket memory and echo latency

Only clean steps are included below; every cell has three completed holds of
at least 30 seconds. KiB means 1,024 bytes. Memory differences at small counts
include substantial warm-up/allocator overhead, especially for A.

| Path | Held connections | Delta KiB/connection | Echo p99 ms |
| --- | ---: | ---: | ---: |
| A: Hornbeam | 1,000 | 156.70 (151.47–163.52) | 136.68 (118.38–150.63) |
| A: Hornbeam | 5,000 | 72.13 (71.04–74.03) | 372.30 (343.02–400.50) |
| B: managed Uvicorn | 1,000 | 41.46 (40.87–41.72) | 184.38 (162.41–218.62) |
| C: Uvicorn ×1 | 1,000 | 42.31 (42.14–42.33) | 30.96 (27.99–47.70) |
| C: Uvicorn ×1 | 5,000 | 42.09 (42.06–42.10) | 39.29 (35.27–43.65) |
| C: Uvicorn ×1 | 10,000 | 41.88 (41.87–41.98) | 44.56 (37.38–45.86) |
| C: Uvicorn ×1 | 20,000 | 41.98 (41.96–41.99) | 69.31 (37.54–84.27) |
| C: Uvicorn ×1 | 30,000 | 41.89 (41.87–41.91) | 93.47 (26.29–104.01) |
| C2: Uvicorn ×2 | 1,000 | 42.26 (42.20–42.57) | 13.76 (13.48–16.60) |
| C2: Uvicorn ×2 | 5,000 | 42.00 (41.93–42.02) | 22.24 (14.11–28.58) |
| C2: Uvicorn ×2 | 10,000 | 41.93 (41.92–42.08) | 19.79 (16.34–21.30) |
| C2: Uvicorn ×2 | 20,000 | 41.88 (41.88–41.92) | 81.08 (77.75–92.11) |
| C2: Uvicorn ×2 | 30,000 | 41.82 (41.81–41.84) | 15.12 (14.81–17.93) |

These loopback measurements do not establish network-facing capacity or
application-level parallelism. The native-runtime readiness limits, not memory,
were the first observed blockers for A and B in this configuration.
