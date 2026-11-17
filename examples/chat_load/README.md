# Chat under load

This example runs the same chat server in two ways and drives both with one
traffic simulator:

- `app.py`: one PyBEAM process. Room counters, room pub/sub, and background
  jobs use PyBEAM services and `TaskSupervisor`.
- `conventional/`: the usual multi-service layout. Two app replicas behind a
  reverse proxy with sticky WebSocket sessions, a KV cache for counters and
  pub/sub, a message broker, and a task queue worker.

Both servers share `protocol.py`, `rooms.py` and `jobs.py`. Each room keeps one
subscription per server process and fans pushes out to its local WebSockets.
A client that falls 256 pushes behind is closed with code 1013.

## Run the PyBEAM server

```console
$ mise run example:chat
```

## Run the conventional stack

The stack needs Docker Compose. It listens on port 8801.

```console
$ docker compose -f examples/chat_load/conventional/compose.yaml up -d --build
```

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

## Differences that affect the numbers

- PyBEAM admits at most 64 running jobs and rejects more with HTTP 503. The
  task queue accepts every job and lets the broker queue grow.
- PyBEAM jobs run in their own interpreter (`ExecutionMode.OWNGIL`). Queue
  workers are separate processes.
- Neither stack persists room counters or retries jobs. A job lost to a crash
  stays lost in both. Counters in PyBEAM disappear when the process stops.
