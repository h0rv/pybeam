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

Add measurements here after running both stacks with the same input and recording
the host, runtime versions, worker concurrency and task unit.
