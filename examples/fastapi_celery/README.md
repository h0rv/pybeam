> `original/` is vendored from [testdrivenio/fastapi-celery](https://github.com/testdrivenio/fastapi-celery) at commit `191ff138701777838c363772253f73a276c16d68`, under its preserved [MIT license](original/LICENSE).

# Background tasks

Pick Short, Medium or Long to submit a blocking task and see its ID, status and
result. Each task sleeps for 10, 20 or 30 seconds and returns `true`.
The port keeps the original HTTP contract, replacing the separate task queue
and KV store with supervised tasks and in-memory shared state.

## Run the port

From the repository root, with an installed runtime or a configured
[development runtime](../../docs/development.md#use-a-development-runtime):

```sh
uv run --locked --extra fastapi --extra hornbeam python -m examples.fastapi_celery.app --port 8810 --task-unit 1
```

Open <http://127.0.0.1:8810/>. The page receives updates by server-sent events
instead of polling. `--task-unit` sets the seconds per task type; its default is
10. `mise run example:fastapi-celery` runs with the defaults.

## Run the original stack

The original needs a container engine with Compose:

```sh
TASK_UNIT=1 docker compose -f examples/fastapi_celery/original/compose.yaml up --build
```

Open <http://127.0.0.1:8811/>; the task dashboard is at
<http://127.0.0.1:5556/>. `TASK_UNIT` defaults to 10.
`mise run example:fastapi-celery-original` runs the same command with defaults.
The worker uses four processes. The port admits at most 64 concurrent tasks and
returns HTTP 503 when full; it does not provide a waiting queue.

Stop containers without deleting the queue volume:

```sh
docker compose -f examples/fastapi_celery/original/compose.yaml down
```

## Compare with the same input

Keep both servers running with the same task unit, then run:

```sh
uv run --locked --extra fastapi python -m examples.fastapi_celery.compare http://127.0.0.1:8810 --tasks 10 --type 1 --task-unit 1
uv run --locked --extra fastapi python -m examples.fastapi_celery.compare http://127.0.0.1:8811 --tasks 10 --type 1 --task-unit 1 --json
```

The driver submits tasks concurrently and polls both stacks every 100 ms. It
reports submit latency and completion overhead beyond the expected sleep in
milliseconds (p50/p99), plus failed requests, failed tasks and timeouts. Completion
overhead includes queueing, network time and polling delay; it is not pure runtime
overhead. `--timeout` bounds each task's observation window (default 300 seconds).
`--json` emits only JSON.

On Linux, repeat `--pid PID` for the stack's root processes to sample peak total
RSS and peak process count, including descendants. For containers, include the
host PIDs of all four services; a client or Compose command PID does not measure
the containers. Run the driver outside those trees. Memory counts sum RSS, so
shared pages can be counted more than once. Results depend on worker concurrency,
task count and host load; this example does not equalize scheduling policies.

## What maps to what

| Original piece | Port |
| --- | --- |
| Task queue broker | `TaskSupervisor` admission; no durable waiting queue |
| KV result backend | `Beam.store()` in-memory shared state, one-day result TTL |
| Worker process | `TaskSupervisor` with a dedicated loop per task |
| One-second browser polling | Per-task pub/sub invalidations and fresh reads over SSE |
| Task dashboard | None; task status is available through the page and HTTP endpoint |

`POST /tasks` accepts `{"type": 1}`, `2` or `3` and returns HTTP 201 with
`task_id`. `GET /tasks/{task_id}` returns `task_id`, `task_status` and
`task_result`: `PENDING`/`null` while unknown or unfinished, `SUCCESS`/`true` on
completion, or `FAILURE` with an error string if the task crashes.
`GET /tasks/{task_id}/events` streams HTML cells for the port's table.

The original task queue and KV store keep queued tasks across a worker restart
and can scale workers to other hosts. That does not guarantee recovery of a task
already executing: this original uses early acknowledgement. The port loses tasks
and results when the PyBEAM process dies. It has no retries, durable queue or
cross-host task dispatch. See the [durable-task prototype](../../docs/guide/durable-tasks.md)
for a separate persistence experiment, not a guarantee of this example.
Blocking task bodies cannot be interrupted promptly during sleep; shutdown may
wait for them. Neither page has authentication; bind only to loopback for local use.

## Measured results

Each stack received 64 concurrent type-1 tasks with a one-second task unit, three
times. Runs alternated between stacks without restarting them after HTTP smoke
checks. Each value below is the median of the three run-level measurements, not
a percentile of pooled requests. Idle snapshots were taken immediately before
each run.

The original's prefork worker defaults to the CPU count (8 on this machine), but
the checked-in Compose command explicitly uses 4. For this comparison, an override
sets 64 worker processes to match the port's 64 concurrent task slots. This lets
both stacks overlap the 64 blocking sleeps; it does not compare CPU-bound work or
make their durability guarantees equivalent. All four original services,
including the dashboard, are included in resource measurements.

| Metric | PyBEAM port | Original, 64 workers |
| --- | ---: | ---: |
| Idle summed RSS (MiB) | 179.3 | 4,893.2 |
| Idle processes | 3 | 68 |
| Peak summed RSS during run (MiB) | 184.2 | 4,900.8 |
| Peak processes during run | 3 | 68 |
| Server CPU time during run (seconds) | 1.80 | 1.18 |
| Submit latency p50 (ms) | 165.430 | 143.470 |
| Submit latency p99 (ms) | 183.946 | 181.475 |
| Completion overhead p50 (ms) | 197.850 | 181.496 |
| Completion overhead p99 (ms) | 272.315 | 267.213 |
| Failures per 64 tasks | 0 | 0 |

Summed RSS counts shared pages once per process, including prefork workers; it is
not unique physical memory. Sampling covers server process trees, not the load
driver or container daemon. These short runs include queueing and 100 ms polling
delay and do not establish sustained throughput.

To rerun, start the port with `--port 8810 --task-unit 1` as above. Start the
original with this override, leaving its Compose file unchanged:

```sh
TASK_UNIT=1 docker compose -f examples/fastapi_celery/original/compose.yaml -f - up -d --build <<'YAML'
services:
  worker:
    command: celery -A worker.celery worker --loglevel=info --concurrency=64
YAML
```

Wait for the worker's `ready` log, then make the same HTTP smoke checks against
both stacks. In Bash, from the repository root, capture the actual Python process
and every container's host PID, then alternate three runs:

```sh
PORT_PID=$(pgrep -nf 'python -m examples[.]fastapi_celery[.]app --port 8810 --task-unit 1')
mapfile -t ORIGINAL_PIDS < <(docker compose -f examples/fastapi_celery/original/compose.yaml ps -q | xargs docker inspect -f '{{.State.Pid}}')
ORIGINAL_ARGS=()
for pid in "${ORIGINAL_PIDS[@]}"; do ORIGINAL_ARGS+=(--pid "$pid"); done

idle() {
  uv run --locked --all-extras python -c 'import json; import sys; from examples.chat_load.traffic import usage; processes, rss, _ = usage(map(int, sys.argv[1:])); print(json.dumps({"processes": processes, "rss_mib": round(rss, 1)}))' "$@"
}

for trial in 1 2 3; do
  printf '\nPort trial %s\n' "$trial"
  idle "$PORT_PID"
  uv run --locked --all-extras python -m examples.fastapi_celery.compare http://127.0.0.1:8810 --tasks 64 --type 1 --task-unit 1 --pid "$PORT_PID" --json
  printf '\nOriginal trial %s\n' "$trial"
  idle "${ORIGINAL_PIDS[@]}"
  uv run --locked --all-extras python -m examples.fastapi_celery.compare http://127.0.0.1:8811 --tasks 64 --type 1 --task-unit 1 "${ORIGINAL_ARGS[@]}" --json
done
```

Machine: Linux, 8 logical CPUs, 32 GiB RAM; Python 3.14.10.
