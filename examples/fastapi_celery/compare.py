import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import Literal

import httpx
from pydantic import ValidationError

from examples.chat_load.traffic import Latency, Resources, sample, summarize
from examples.fastapi_celery.app import Accepted, Status


@dataclass(frozen=True, slots=True)
class Plan:
    url: str
    tasks: int = 10
    type: Literal[1, 2, 3] = 1
    task_unit: float = 10
    timeout: float = 300
    poll_interval: float = 0.1


@dataclass(frozen=True, slots=True)
class Outcome:
    submit_ms: float | None
    overhead_ms: float | None
    failed: bool


@dataclass(frozen=True, slots=True)
class Report:
    plan: Plan
    submit_ms: Latency | None
    completion_overhead_ms: Latency | None
    failures: int
    resources: Resources | None


async def one(client: httpx.AsyncClient, plan: Plan) -> Outcome:
    started = time.perf_counter()
    submitted: float | None = None
    try:
        async with asyncio.timeout(plan.timeout):
            response = await client.post("/tasks", json={"type": plan.type})
            response.raise_for_status()
            accepted = Accepted.model_validate_json(response.content)
            submitted = (time.perf_counter() - started) * 1000
            while True:
                response = await client.get("/tasks/" + accepted.task_id)
                response.raise_for_status()
                status = Status.model_validate_json(response.content)
                if status.task_status == "FAILURE":
                    return Outcome(submitted, None, True)
                if status.task_status == "SUCCESS":
                    elapsed = time.perf_counter() - started
                    return Outcome(
                        submitted,
                        (elapsed - plan.type * plan.task_unit) * 1000,
                        status.task_result is not True,
                    )
                await asyncio.sleep(plan.poll_interval)
    except httpx.HTTPError, ValidationError, TimeoutError:
        return Outcome(submitted, None, True)


async def compare(plan: Plan, pids: list[int]) -> Report:
    done = asyncio.Event()
    async with asyncio.TaskGroup() as tasks:
        sampler = tasks.create_task(sample(pids, done))
        try:
            async with httpx.AsyncClient(base_url=plan.url, timeout=30) as client:
                results = [tasks.create_task(one(client, plan)) for _ in range(plan.tasks)]
                outcomes = [await result for result in results]
        finally:
            done.set()
    return Report(
        plan,
        summarize([item.submit_ms for item in outcomes if item.submit_ms is not None]),
        summarize([item.overhead_ms for item in outcomes if item.overhead_ms is not None]),
        sum(item.failed for item in outcomes),
        sampler.result(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure the same task contract on either stack")
    parser.add_argument("url")
    parser.add_argument("--tasks", type=int, default=10)
    parser.add_argument("--type", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--task-unit", type=float, default=10)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--pid", type=int, action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.tasks <= 0 or not 0 < args.task_unit <= 3600 or not 0 < args.timeout <= 86_400:
        parser.error(
            "tasks must be positive; task-unit <= 3600 and timeout <= 86400, both positive"
        )
    kind: Literal[1, 2, 3] = 1
    if args.type == 2:
        kind = 2
    elif args.type == 3:
        kind = 3
    plan = Plan(args.url, args.tasks, kind, args.task_unit, args.timeout)
    report = asyncio.run(compare(plan, args.pid))
    if args.json:
        print(json.dumps(asdict(report), indent=2))
        return
    for label, latency in (
        ("Submit", report.submit_ms),
        ("Completion overhead", report.completion_overhead_ms),
    ):
        if latency is None:
            print(label + ": no samples")
        else:
            print("{}: p50={} ms p99={} ms".format(label, latency.p50, latency.p99))
    print("Failures: {} / {}".format(report.failures, plan.tasks))
    if report.resources is not None:
        print(
            "Peak total RSS: {} MiB; peak process count: {}".format(
                report.resources.peak_rss_mb, report.resources.processes
            )
        )


if __name__ == "__main__":
    main()
