import json
import subprocess
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
import pytest
from pydantic import BaseModel, Field, TypeAdapter

from examples.agents.protocol import Scenario
from examples.agents.usual.launch import URL, compose, kill, running
from examples.agents.usual.models import Turn, View

type Case = Literal["stop", "hang", "burn", "crash", "kill"]


class DockerStat(BaseModel):
    name: str = Field(alias="Name")
    memory: str = Field(alias="MemUsage")
    pids: str = Field(alias="PIDs")


@dataclass
class Stack:
    client: httpx.Client

    def snapshots(self) -> list[Turn]:
        response = self.client.get(URL + "/api/sessions")
        response.raise_for_status()
        return TypeAdapter(list[Turn]).validate_json(response.content)

    def start(self, mode: Scenario, count: int = 1) -> list[str]:
        previous = {turn.id for turn in self.snapshots()}
        response = self.client.post(URL + "/sessions", data={"mode": mode, "count": str(count)})
        assert response.status_code == 303, response.text
        return [turn.id for turn in self.snapshots() if turn.id not in previous]

    def view(self, session: str) -> View:
        response = self.client.get(URL + "/api/sessions/" + session)
        response.raise_for_status()
        return View.model_validate_json(response.content)

    def wait(self, session: str, condition: Callable[[View], bool], timeout: float = 20) -> View:
        deadline = time.monotonic() + timeout
        while True:
            value = self.view(session)
            if condition(value):
                return value
            assert time.monotonic() < deadline, value.model_dump_json()
            time.sleep(0.05)

    def stop(self, session: str) -> View:
        response = self.client.post(URL + "/sessions/" + session + "/stop")
        assert response.status_code == 303
        return self.wait(session, lambda value: value.snapshot.state == "stopped")

    def completed(self, sessions: list[str], timeout: float = 150) -> list[View]:
        deadline = time.monotonic() + timeout
        while True:
            snapshots = {value.id: value for value in self.snapshots()}
            if all(snapshots[session].state == "completed" for session in sessions):
                return [self.view(session) for session in sessions]
            assert time.monotonic() < deadline, {
                session: snapshots[session].state for session in sessions
            }
            time.sleep(0.1)


@pytest.fixture
def stack(request: pytest.FixtureRequest, tmp_path: Path) -> Generator[Stack]:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and Docker")
    with running(), httpx.Client(timeout=15) as client:
        try:
            yield Stack(client)
        finally:
            (tmp_path / "containers.log").write_text(compose("logs", "--no-color"))


def resources() -> tuple[list[DockerStat], dict[str, int]]:
    containers = compose("ps", "-q").splitlines()
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    stats = [DockerStat.model_validate_json(line) for line in result.stdout.splitlines()]
    processes = {}
    for container in containers:
        result = subprocess.run(
            ["docker", "top", container, "-eo", "pid"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        processes[container] = len(result.stdout.splitlines()) - 1
    return stats, processes


def completion_times(values: list[View], submitted: float) -> list[float]:
    return sorted(round(value.snapshot.token_times[-1] - submitted, 3) for value in values)


def gap(values: list[View]) -> float:
    return max(
        later - earlier
        for value in values
        for earlier, later in pairwise(value.snapshot.token_times)
    )


@pytest.mark.runtime
@pytest.mark.parametrize("case", ["stop", "hang", "burn", "crash", "kill"])
def test_conventional_faults_with_twelve_healthy_sessions(stack: Stack, case: Case) -> None:
    baseline_started = time.monotonic()
    baseline_ids = stack.start("normal", 12)
    baseline = stack.completed(baseline_ids)
    assert all(value.counts == {"step-1": 1, "step-2": 1, "step-3": 1} for value in baseline)
    bad = stack.start(case if case in ("burn", "crash") else "hang")[0]
    active = stack.wait(bad, lambda value: bool(value.executions.get("step-2")))
    started = time.monotonic()
    healthy_ids = stack.start("normal", 12)
    stopped_ms: float | None = None
    killed_at: float | None = None
    if case == "kill":
        stack.wait(healthy_ids[0], lambda value: bool(value.executions.get("step-1")))
        killed_at = time.monotonic()
        kill(UUID(bad))
    stats, processes = resources()
    if case == "stop":
        before = time.monotonic()
        stopped = stack.stop(bad)
        stopped_ms = round((time.monotonic() - before) * 1000, 3)
        assert stopped.counts == {"step-1": 1, "step-2": 1}
        worker = active.snapshot.node
        assert worker in ("worker-1", "worker-2")
        pid = active.executions["step-2"][0].pid
        gone = compose("exec", "-T", worker, "sh", "-c", "test ! -e /proc/" + str(pid))
        assert gone == ""
    healthy = stack.completed(healthy_ids)
    if case == "burn":
        fault = stack.wait(bad, lambda value: value.snapshot.state == "completed", timeout=40)
        assert "CPU work finished" in fault.snapshot.ledger["step-2"].result
    elif case == "kill":
        fault = stack.wait(
            bad, lambda value: len(value.executions.get("step-2", [])) >= 2, timeout=130
        )
        assert fault.snapshot.node != active.snapshot.node
        assert fault.counts["step-1"] >= 2
    elif case == "crash":
        fault = stack.wait(bad, lambda value: value.counts.get("step-1", 0) >= 2)
        logs = compose("logs", "--no-color", "worker-1", "worker-2")
        assert "WorkerLostError" in logs and "SIGSEGV" in logs
        fault = stack.stop(bad)
    else:
        fault = stack.view(bad)
    if case == "hang":
        assert fault.snapshot.state == "running" and fault.counts == {"step-1": 1, "step-2": 1}
    for value in healthy:
        assert value.snapshot.state == "completed"
        assert all(count >= 1 for count in value.counts.values())
        if case != "kill":
            assert value.counts == {"step-1": 1, "step-2": 1, "step-3": 1}
    redis_counts = compose(
        "exec", "-T", "redis", "redis-cli", "--json", "HGETALL", "agents:counts:" + bad
    )
    assert TypeAdapter(dict[str, int]).validate_json(redis_counts) == fault.counts
    if fault.snapshot.state == "stopped":
        assert sum(entry.kind == "stopped" for entry in fault.snapshot.transcript) == 1
    repeat_after = (
        None if killed_at is None else round(fault.executions["step-1"][1].entered - killed_at, 3)
    )
    print(
        json.dumps(
            {
                "case": case,
                "baseline_completion_s": completion_times(baseline, baseline_started),
                "healthy_completion_s": completion_times(healthy, started),
                "max_token_gap_s": round(gap(healthy), 6),
                "fault_counts": fault.counts,
                "fault_state": fault.snapshot.state,
                "fault_attempts": fault.snapshot.attempts,
                "healthy_counts": {value.snapshot.id: value.counts for value in healthy},
                "stop_ms": stopped_ms,
                "repeat_after_kill_s": repeat_after,
                "docker_stats": [value.model_dump() for value in stats],
                "processes_total": sum(processes.values()),
            }
        ),
        flush=True,
    )
    if fault.snapshot.state == "running":
        stack.stop(bad)


@pytest.mark.runtime
def test_conventional_empty_errors_and_sse_reconnect(stack: Stack) -> None:
    assert stack.snapshots() == []
    assert stack.client.get(URL + "/").status_code == 200
    assert stack.client.get(URL + "/api/sessions/" + "0" * 32).status_code == 404
    assert stack.client.post(URL + "/sessions", data={"count": "0"}).status_code == 422
    assert (
        stack.client.post(
            URL + "/sessions", headers={"Origin": "https://wrong.invalid"}
        ).status_code
        == 403
    )
    session = stack.start("normal")[0]
    stack.completed([session])
    with stack.client.stream("GET", URL + "/sessions/" + session + "/events") as response:
        assert response.status_code == 200
        event = ""
        for line in response.iter_lines():
            if not line:
                break
            event += line
        assert "completed" in event
