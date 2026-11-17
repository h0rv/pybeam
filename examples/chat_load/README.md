# Chat under load

This example runs the same chat server in two ways and drives both with one
traffic simulator:

- `app.py`: one PyBEAM process. Room counters, room pub/sub, and background
  jobs use PyBEAM services and `TaskSupervisor`.
- `conventional/`: eight web workers, a KV cache for counters and pub/sub,
  a message broker, and a task-queue worker.

Both servers share `protocol.py`, `rooms.py` and `jobs.py`. Each room keeps one
subscription per server process and fans pushes out to its local WebSockets.
A client that falls 256 pushes behind is closed with code 1013.

## Simulate traffic

Pass the server URL. `--pid` samples a server process tree's CPU and memory
from `/proc` (Linux only); repeat it for each container.

```console
$ mise run example:chat-load -- http://127.0.0.1:8800 --pid "$(pgrep -f examples.chat_load.app)"
$ mise run example:chat-load -- http://127.0.0.1:8801 \
    $(docker compose -f examples/chat_load/conventional/compose.yaml ps -q | xargs docker inspect -f '--pid {{.State.Pid}}')
```

Each client sends Poisson-timed messages without waiting for replies, and the
simulator posts background jobs at a fixed average rate. It reports measured
values only:

- acknowledgement, fan-out and job completion latency percentiles
- deliveries against the expected count (every client in a room sees each message)
- counter mismatches between the server's room counters and what clients saw
- rejected jobs, connection errors, and server CPU, memory and process count

Use `--json report.json` to keep the full report. Run `--help` for the load
options.

## Crash test

With a compatible runtime installed, run the servers and dashboard in separate
terminals. Use a configured 3.14.10t runtime to reproduce the measurements below.

```console
$ mise run example:chat
$ docker compose -f examples/chat_load/conventional/compose.yaml up -d --build
$ uv run --locked --extra fastapi python -m examples.chat_load.crash_test
```

Open `http://127.0.0.1:8802`; servers use ports 8800/8801. The dashboard also
starts with `mise run example:crash-test`. Flags: `--usual-url`, `--pybeam-url`,
`--rooms`, `--clients`, `--rate` (messages/s/socket). Traffic runs once per
dashboard process, not per tab. Choose **Crash Room 1 on both**. After a 4 s
countdown, the same native crash runs on both stacks concurrently. Watch the
chat messages in the other rooms: does a user lose their connection, or keep
getting replies? Each test watches 10 s of traffic, then allows 5 s for recovery.
The default rate is one message per second per user so the conversation is
readable. The button waits for live replies from every user on both stacks.

The view selects a user who actually disconnected or waited for a reply, then
shows the matching user on both sides. It retains the interruption and other-room
disconnect counts after connections recover. “Confirmed” means the server
acknowledged a send. An unconfirmed send might have reached the server; clients
do not replay it. A run with no disconnects does not establish zero latency impact.

Open **How this works** to try a CPU stall or raised exception on both stacks.
Each is a separate paired test; only the latest result is retained. A run already
in progress rejects another start. Keep this local.

The run targets `room-1` through `POST /rooms/{room}/faults/{kind}`:

| Fault | Usual stack | PyBeam |
| --- | --- | --- |
| `raise` | Handler raises; HTTP 500 and failed notice | Dedicated-loop task raises; HTTP 200 and contained notice |
| `hog` | Tight loop blocks one web worker for 3 s | Dedicated-loop task runs the same loop; completed notice |
| `abort` | Worker dies; request loses its response; replacement worker starts | Isolated child dies; HTTP 200 and contained notice |

The fault target is separate from the other-room conversation. Its request
outcome is not a measurement of other users' experience. A lost fault request
alone does not prove a worker died. Results depend on worker assignment and
this machine. The usual stack runs faults in its web workers; PyBeam runs native
crashes in isolated child processes. This compares those failure boundaries,
not maximum throughput. The measurements below used a separate load runner.

### Measured limits

Linux x86-64, 8 CPUs shared with the loopback client; dashboard stopped,
stacks loaded sequentially. Three trials per point; medians [min–max].
PyBeam: 3.14.10t, dedicated-loop tasks. Usual stack: 3.14.10, eight web workers
and a KV cache.

Fault trials: 8 rooms × 4 sockets × 4 messages/s, no jobs. Paired 3 s baseline
and fault windows; fault windows start at the client POST, including dispatch.
Only rooms 2–8 sends scheduled in the window count. Late replies retain full
latency; missing replies after an 8 s drain count as unbounded, including
sends missed during reconnection.

| Other-room ack p99, ms: baseline → fault | Usual stack | PyBeam |
| --- | --- | --- |
| `hog` | 4.52 [4.19–4.59] → 2935.94 [2615.46–3003.46] | 3.63 [2.19–99.64] → 4.95 [3.27–5.93] |
| `abort` | 3.32 [3.27–4.51] → unbounded [3.74–unbounded] | 4.28 [3.61–4.46] → 4.52 [2.86–8.73] |
| `raise` | 2.72 [2.67–6.63] → 4.24 [2.58–12.32] | 5.11 [2.85–49.00] → 3.77 [3.46–5.98] |

Hog-window maximum pending age was 2962.41 [2905.37–2976.08] ms versus
2.51 [1.29–4.16] ms. Abort disconnected 9 [3–9] of 28 other-room sockets
versus zero, with 12 [3–12] versus zero scheduled sends unacknowledged.
All post-fault health checks returned 200; other-room HTTP probes had zero
failures. PyBeam's paired hog p99 changes (+2.30, +1.08, −94.68 ms) do **not**
establish zero latency impact.

Throughput: `traffic.py`, 8 rooms × 4 sockets, 64-byte messages, 15 s runs,
10 s drain, 20 checksum jobs/s of size 200,000. Rates include completions
through the drain divided by active send time (jobs use the nominal 15 s).
Highest tested point with all
messages, fan-out and jobs delivered, no errors, and ack p99 ≤100 ms in all
three runs—not a server-only ceiling:

| Stack | Acknowledged messages/s | Ack p99, ms | Completed jobs/s |
| --- | --- | --- | --- |
| Usual stack | 3844 [3767–3884] | 21.05 [13.77–35.69] | 20.13 [19.80–20.27] |
| PyBeam | 1989 [1969–1997] | 11.61 [8.84–14.36] | 21.20 [20.73–21.40] |

Nominal rates: 4096/s and 2048/s. Requiring ≥95% nominal delivery in every run
limits both to the 2048/s point. PyBeam disconnected clients at 4096/s in two
of three runs, and at 8192/s in all three. The usual stack delivered all replies
at nominal 16384/s, but p99 reached 7892 [7005–8337] ms: draining a backlog is
not sustained throughput. Client CPU/pacing is shared; lower-load latency
outliers also occurred. These short-run points are not capacity guarantees.

### Application and service changes

App code changes **205 lines: +130/−75**, not ten:
`git diff --no-index conventional/app.py app.py` gives +130/−52; deleting
the 23-line `conventional/worker.py` adds −23. Shared modules are excluded.
App code grows from 190 to 245 lines. Four compose services become one runtime:
the KV cache, message broker and task-queue worker are removed. Eight web
workers become one application process, plus a transient isolated abort child
and a separate development launcher.

## Differences that affect the numbers

- PyBEAM admits at most 64 running jobs and rejects more with HTTP 503. The
  task queue accepts every job and lets the broker queue grow.
- PyBEAM jobs use dedicated-loop worker tasks. Task-queue workers are
  separate processes.
- Neither stack persists room counters or retries jobs. A job lost to a crash
  stays lost in both. Counters in PyBEAM disappear when the process stops.
