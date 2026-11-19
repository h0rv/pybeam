import os
import platform
import statistics
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from itertools import pairwise
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
import psutil
from pydantic import BaseModel, Field, TypeAdapter

from examples.agents.launch import ROOT, running
from examples.agents.protocol import TERMINAL, Execution, LedgerEntry, Scenario, Status
from examples.agents.usual.launch import URL, compose, kill
from examples.agents.usual.launch import running as usual_running

type Case = Literal["stop", "hang", "burn", "crash", "kill"]
type StackName = Literal["PyBEAM", "Redis/Celery"]
CASES: tuple[Case, ...] = ("stop", "hang", "burn", "crash", "kill")
RESULTS = Path(__file__).with_name("results")
KILL_TIMEOUT_S = 600


class Snapshot(BaseModel):
    id: str
    node: str
    state: Status
    ledger: dict[str, LedgerEntry] = Field(default_factory=dict)
    token_times: list[float] = Field(default_factory=list)


class View(BaseModel):
    snapshot: Snapshot
    executions: dict[str, list[Execution]]

    @property
    def counts(self) -> dict[str, int]:
        return {key: len(events) for key, events in self.executions.items()}


class Resources(BaseModel):
    processes: int
    rss_mib: float
    docker_stats: list[dict[str, str]] = Field(default_factory=list)


class Measurement(BaseModel):
    stack: StackName
    case: Case
    baseline_completion_s: list[float]
    healthy_completion_s: list[float]
    median_relative_to_baseline: float
    max_relative_to_baseline: float
    max_token_gap_s: float
    healthy_repeats: int
    fault_repeats: int
    fault_state: Status
    stop_s: float | None
    kill_to_resume_s: float | None
    kill_to_complete_s: float | None
    healthy_counts: dict[str, dict[str, int]]
    fault_counts: dict[str, int]
    resources: Resources


class Report(BaseModel):
    measured_at: datetime
    machine: str
    cpu_count: int | None
    healthy_sessions: int = 12
    notes: list[str]
    measurements: list[Measurement]


def repeats(values: list[View]) -> int:
    return sum(max(count - 1, 0) for value in values for count in value.counts.values())


def token_gap(values: list[View]) -> float:
    return max(
        (
            later - earlier
            for value in values
            for earlier, later in pairwise(value.snapshot.token_times)
        ),
        default=0,
    )


@dataclass
class Stack:
    name: StackName
    client: httpx.Client

    def snapshots(self) -> list[Snapshot]:
        response = self.client.get("/api/sessions")
        response.raise_for_status()
        return TypeAdapter(list[Snapshot]).validate_json(response.content)

    def start(self, scenario: Scenario, count: int) -> list[str]:
        previous = {value.id for value in self.snapshots()}
        response = self.client.post(
            "/sessions", data={"mode": scenario, "count": str(count), "placement": "a"}
        )
        if response.status_code != 303:
            raise RuntimeError(response.text)
        sessions = [value.id for value in self.snapshots() if value.id not in previous]
        if len(sessions) != count:
            raise RuntimeError("Session creation returned an unexpected batch size")
        return sessions

    def view(self, session: str) -> View:
        response = self.client.get("/api/sessions/" + session)
        response.raise_for_status()
        return View.model_validate_json(response.content)

    def wait(self, session: str, predicate: Callable[[View], bool]) -> View:
        deadline = time.monotonic() + 180
        while True:
            value = self.view(session)
            if predicate(value):
                return value
            if time.monotonic() >= deadline:
                raise TimeoutError(value.model_dump_json())
            time.sleep(0.05)

    def stop(self, session: str) -> None:
        response = self.client.post("/sessions/" + session + "/stop")
        if response.status_code != 303:
            raise RuntimeError(response.text)
        self.wait(session, lambda value: value.snapshot.state in TERMINAL)

    def healthy(self) -> list[float]:
        submitted = time.monotonic()
        sessions = self.start("normal", 12)
        finished: dict[str, float] = {}
        deadline = submitted + 180
        while len(finished) < len(sessions):
            self.observe(sessions, finished, submitted)
            if time.monotonic() > deadline:
                raise TimeoutError("Healthy batch did not finish")
            time.sleep(0.05)
        return sorted(finished.values())

    def observe(self, sessions: list[str], finished: dict[str, float], submitted: float) -> None:
        for value in self.snapshots():
            if value.id in sessions and value.id not in finished and value.state in TERMINAL:
                if value.state != "completed":
                    raise RuntimeError(
                        "Healthy session did not complete: " + value.model_dump_json()
                    )
                finished[value.id] = time.monotonic() - submitted


def resources(stack: StackName) -> Resources:
    stats: list[dict[str, str]] = []
    if stack == "PyBEAM":
        pids = [process.pid for process in psutil.Process().children(recursive=True)]
    else:
        containers = compose("ps", "-q").splitlines()
        output = subprocess.run(  # noqa: S603
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        stats = [TypeAdapter(dict[str, str]).validate_json(line) for line in output.splitlines()]
        pids = []
        for container in containers:
            try:
                output = subprocess.run(  # noqa: S603
                    ["docker", "top", container, "-eo", "pid"],  # noqa: S607
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout
            except subprocess.CalledProcessError:
                state = subprocess.run(  # noqa: S603
                    ["docker", "inspect", "--format", "{{.State.Running}}", container],  # noqa: S607
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout.strip()
                if state == "false":
                    continue
                raise
            pids.extend(int(line.strip()) for line in output.splitlines()[1:])
    rss, count = 0, 0
    for pid in pids:
        try:
            rss += psutil.Process(pid).memory_info().rss
            count += 1
        except psutil.NoSuchProcess:
            continue
    return Resources(processes=count, rss_mib=rss / 1024**2, docker_stats=stats)


def kill_owner(stack: Stack, fault: str, owner: str) -> None:
    if stack.name == "Redis/Celery":
        kill(UUID(fault))
    else:
        port = {"a": 8810, "b": 8811, "c": 8812}[owner]
        try:
            response = httpx.post("http://127.0.0.1:" + str(port) + "/node/kill", timeout=10)
            response.raise_for_status()
        except httpx.RemoteProtocolError:
            pass


def measure(stack: Stack, case: Case, sampling: ThreadPoolExecutor) -> Measurement:
    stack.healthy()
    baseline = stack.healthy()
    scenario: Scenario = case if case in ("burn", "crash") else "burn" if case == "kill" else "hang"
    fault = stack.start(scenario, 1)[0]
    active = stack.wait(fault, lambda value: bool(value.executions.get("step-2")))
    submitted = time.monotonic()
    sessions = stack.start("normal", 12)
    sample = sampling.submit(resources, stack.name)
    stopped: float | None = None
    killed: float | None = None
    resumed: float | None = None
    completed: float | None = None
    if case == "stop":
        before = time.monotonic()
        stack.stop(fault)
        stopped = time.monotonic() - before
    elif case == "kill":
        stack.wait(sessions[0], lambda value: bool(value.executions.get("step-1")))
        killed = time.monotonic()
        kill_owner(stack, fault, active.snapshot.node)
    finished: dict[str, float] = {}
    deadline = time.monotonic() + (KILL_TIMEOUT_S if case == "kill" else 180)
    while True:
        stack.observe(sessions, finished, submitted)
        current = stack.view(fault)
        now = time.monotonic()
        if killed is not None:
            if resumed is None and current.snapshot.node != active.snapshot.node:
                resumed = now - killed
            if completed is None and current.snapshot.state == "completed":
                completed = now - killed
        fault_done = case not in ("kill", "burn") or current.snapshot.state in TERMINAL
        if len(finished) == 12 and fault_done:
            break
        if now >= deadline:
            raise TimeoutError("Fault batch did not finish: " + current.model_dump_json())
        time.sleep(0.05)
    values = [stack.view(session) for session in sessions]
    fault_view = current
    times = sorted(finished.values())
    result = Measurement(
        stack=stack.name,
        case=case,
        baseline_completion_s=baseline,
        healthy_completion_s=times,
        median_relative_to_baseline=statistics.median(times) / statistics.median(baseline),
        max_relative_to_baseline=max(times) / max(baseline),
        max_token_gap_s=token_gap(values),
        healthy_repeats=repeats(values),
        fault_repeats=repeats([fault_view]),
        fault_state=fault_view.snapshot.state,
        stop_s=stopped,
        kill_to_resume_s=resumed,
        kill_to_complete_s=completed,
        healthy_counts={value.snapshot.id: value.counts for value in values},
        fault_counts=fault_view.counts,
        resources=sample.result(),
    )
    if current.snapshot.state not in TERMINAL:
        stack.stop(fault)
    return result


def chart(report: Report, title: str, unit: str, value: Callable[[Measurement], float]) -> str:
    rows = report.measurements
    maximum = max((value(row) for row in rows), default=0) or 1
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 720 470" role="img" aria-labelledby="title">',
        '<title id="title">' + escape(title) + "</title>",
        "<style>svg{background:#fff;color:#202124;font:15px system-ui,sans-serif}text{fill:currentColor}.py{fill:#246a9b}.usual{fill:#9a4c15}@media(prefers-color-scheme:dark){svg{background:#181a1b;color:#eee}.py{fill:#83bee7}.usual{fill:#efb180}}</style>",
        '<text x="16" y="28">' + escape(title + " (" + unit + ")") + "</text>",
    ]
    for index, row in enumerate(rows):
        y = 60 + index * 39
        width = value(row) / maximum * 365
        parts.extend(
            [
                '<text x="16" y="'
                + str(y + 16)
                + '">'
                + escape(row.case + " · " + row.stack)
                + "</text>",
                '<rect class="'
                + ("py" if row.stack == "PyBEAM" else "usual")
                + '" x="225" y="'
                + str(y)
                + '" width="'
                + str(width)
                + '" height="23"/>',
                '<text x="'
                + str(235 + width)
                + '" y="'
                + str(y + 17)
                + '">'
                + format(value(row), ".2f")
                + "</text>",
            ]
        )
    parts.append("</svg>")
    return "\n".join(parts)


def summary(row: Measurement) -> str:
    return (
        row.case
        + " | "
        + row.stack
        + " | median/max "
        + format(row.median_relative_to_baseline, ".2f")
        + "x/"
        + format(row.max_relative_to_baseline, ".2f")
        + "x | gap "
        + format(row.max_token_gap_s, ".3f")
        + "s | repeats healthy/fault "
        + str(row.healthy_repeats)
        + "/"
        + str(row.fault_repeats)
        + " | "
        + row.fault_state
        + " | stop/resume/complete "
        + "/".join(
            "—" if value is None else format(value, ".3f") + "s"
            for value in (row.stop_s, row.kill_to_resume_s, row.kill_to_complete_s)
        )
        + " | "
        + str(row.resources.processes)
        + " processes "
        + format(row.resources.rss_mib, ".1f")
        + " MiB RSS"
    )


def main() -> None:
    directory = ROOT / "_build" / "agents-compare" / uuid4().hex
    directory.mkdir(parents=True)
    report = Report(
        measured_at=datetime.now(UTC),
        machine=platform.platform(),
        cpu_count=os.cpu_count(),
        notes=[
            "Each case starts fresh stacks. Each stack runs 12 warm-up sessions, then 12 baseline sessions, then 12 healthy sessions beside one fault.",
            "Stacks are measured sequentially on the same host; the other stack is idle. PyBEAM sessions are placed on node a; Celery chooses workers.",
            "Completion and recovery use host monotonic time observed by HTTP polling (50 ms target interval). Gaps use server token timestamps, including lookup pauses, excluding initial queue wait.",
            "Kill interrupts a burn after its first lookup. Healthy sessions have begun their first lookup. Crash retries and hang are stopped after observation; their reported states precede cleanup.",
            "Memory is sampled sum of process RSS, including shared pages more than once, not a peak. PyBEAM includes launchers, private EPMD and descendants, excluding this driver; Celery includes app, workers and Redis. Docker stats are also retained.",
        ],
        measurements=[],
    )
    stacks: tuple[tuple[StackName, str], ...] = (
        ("PyBEAM", "http://127.0.0.1:8811"),
        ("Redis/Celery", URL),
    )
    for case in CASES:
        with (
            (directory / (case + ".log")).open("wb") as output,
            usual_running(),
            running(directory=directory / case, output=output),
        ):
            for name, url in stacks:
                with (
                    httpx.Client(base_url=url, timeout=20) as client,
                    ThreadPoolExecutor(max_workers=1) as sampling,
                ):
                    row = measure(Stack(name, client), case, sampling)
                    report.measurements.append(row)
                    print(summary(row), flush=True)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "comparison.json").write_text(report.model_dump_json(indent=2) + "\n")
    charts: tuple[tuple[str, str, str, Callable[[Measurement], float]], ...] = (
        (
            "completion.svg",
            "Maximum healthy completion time relative to warm baseline",
            "ratio",
            lambda row: row.max_relative_to_baseline,
        ),
        ("gaps.svg", "Maximum healthy token gap", "seconds", lambda row: row.max_token_gap_s),
        (
            "repeats.svg",
            "Repeated tool executions, healthy and fault sessions",
            "executions",
            lambda row: row.healthy_repeats + row.fault_repeats,
        ),
    )
    for filename, title, unit, metric in charts:
        (RESULTS / filename).write_text(chart(report, title, unit, metric))
    print(
        "Results: examples/agents/results/comparison.json and completion.svg, gaps.svg, repeats.svg",
        flush=True,
    )


if __name__ == "__main__":
    main()
