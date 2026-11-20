import os
import signal
import subprocess
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter

from examples.agents.launch import free_port, running
from examples.agents.protocol import Node, Scenario, SessionView, Snapshot

pytestmark = pytest.mark.runtime


@dataclass
class Cluster:
    client: httpx.Client
    ports: tuple[int, int, int]
    processes: list[subprocess.Popen[bytes]]

    def url(self, path: str, node: int = 1) -> str:
        return "http://127.0.0.1:" + str(self.ports[node]) + path

    def snapshots(self) -> list[Snapshot]:
        response = self.client.get(self.url("/api/sessions"))
        response.raise_for_status()
        return TypeAdapter(list[Snapshot]).validate_json(response.content)

    def start(self, mode: Scenario, *, count: int = 1, owner: Node = "a") -> list[str]:
        previous = {snapshot.id for snapshot in self.snapshots()}
        response = self.client.post(
            self.url("/sessions"), data={"mode": mode, "count": str(count), "placement": owner}
        )
        assert response.status_code == 303, response.text
        return [snapshot.id for snapshot in self.snapshots() if snapshot.id not in previous]

    def view(self, session: str, node: int = 1) -> SessionView:
        response = self.client.get(self.url("/api/sessions/" + session, node))
        response.raise_for_status()
        return SessionView.model_validate_json(response.content)

    def wait(
        self, session: str, predicate: Callable[[SessionView], bool], *, timeout: float = 15
    ) -> SessionView:
        deadline = time.monotonic() + timeout
        while True:
            value = self.view(session)
            if predicate(value):
                return value
            assert time.monotonic() < deadline, value.model_dump_json()
            time.sleep(0.03)

    def stop(self, session: str) -> SessionView:
        response = self.client.post(self.url("/sessions/" + session + "/stop"))
        assert response.status_code == 303, response.text
        return self.wait(session, lambda value: value.snapshot.state == "stopped")

    def kill(self, node: int) -> None:
        with suppress(httpx.RemoteProtocolError):
            response = self.client.post(self.url("/node/kill", node))
            response.raise_for_status()
        self.processes[node].wait(timeout=5)
        assert self.processes[node].returncode == -signal.SIGKILL


@contextmanager
def running_cluster(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    *,
    application: str = "examples.agents.app:app",
) -> Generator[Cluster]:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and the agents, fastapi and hornbeam extras")
    ports = (free_port(), free_port(), free_port())
    with (
        (tmp_path / "nodes.log").open("wb") as output,
        running(
            directory=tmp_path,
            ports=ports,
            output=output,
            application=application,
            extra_paths=(Path(__file__).parent / "native",),
        ) as processes,
        httpx.Client(timeout=15) as client,
    ):
        yield Cluster(client, ports, processes)


@pytest.fixture
def cluster(request: pytest.FixtureRequest, tmp_path: Path) -> Generator[Cluster]:
    with running_cluster(request, tmp_path) as current:
        yield current


def streamed_states(response: httpx.Response) -> list[str]:
    states: list[str] = []
    for line in response.iter_lines():
        if line.startswith("data:") and "State:" in line:
            states.append(line)
            if "<strong>completed</strong>" in line:
                return states
    raise AssertionError("SSE ended before completion")


def test_snapshot_and_sse_overflow_leave_sessions_streaming(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    with running_cluster(request, tmp_path, application="agents_overflow_app:app") as current:
        affected, healthy = current.start("normal", count=2)
        with (
            current.client.stream("GET", current.url("/sessions/" + affected + "/events")) as slow,
            current.client.stream("GET", current.url("/sessions/" + healthy + "/events")) as fast,
        ):
            assert slow.status_code == fast.status_code == 200
            assert len(streamed_states(fast)) > 2
            assert len(streamed_states(slow)) > 2
        for node in range(3):
            health = current.client.get(current.url("/health", node))
            assert health.status_code == 200, health.text
        response = current.client.get(current.url("/test/overflows"))
        response.raise_for_status()
        counts = TypeAdapter(dict[str, int]).validate_json(response.content)
        assert counts["snapshot"] > 0 and counts["sse"] > 0, counts
        for session in (affected, healthy):
            value = current.wait(session, lambda value: value.snapshot.state == "completed")
            assert all(len(events) == 1 for events in value.executions.values())
        later = current.start("normal")[0]
        with current.client.stream(
            "GET", current.url("/sessions/" + later + "/events")
        ) as response:
            assert response.status_code == 200
            assert len(streamed_states(response)) > 2
        print("OVERFLOW_RECOVERY", counts, "HEALTHY_STREAMS_COMPLETED", 3)


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_warm_batches_with_sse_keep_snapshot_subscription_alive(cluster: Cluster) -> None:
    for _ in range(3):
        sessions = cluster.start("normal", count=12)
        with cluster.client.stream(
            "GET", cluster.url("/sessions/" + sessions[0] + "/events")
        ) as response:
            assert response.status_code == 200
            assert next(response.iter_lines()).startswith("data:")
        deadline = time.monotonic() + 20
        while True:
            states = {snapshot.id: snapshot.state for snapshot in cluster.snapshots()}
            if all(states[session] == "completed" for session in sessions):
                break
            assert time.monotonic() < deadline, states
            time.sleep(0.05)


def test_stop_terminates_isolated_tool_and_sse_reconnects(cluster: Cluster) -> None:
    session = cluster.start("hang")[0]
    value = cluster.wait(session, lambda value: bool(value.executions.get("step-2")))
    pid = value.executions["step-2"][0].pid
    assert not gone(pid)
    stopped = cluster.stop(session)
    assert gone(pid), "Stop returned before the tool process exited"
    assert stopped.snapshot.ledger["step-1"].state == "done"
    assert stopped.snapshot.ledger["step-2"].state == "interrupted"
    assert all(len(events) == 1 for events in stopped.executions.values())
    with cluster.client.stream(
        "GET", cluster.url("/sessions/" + session + "/events", 2)
    ) as response:
        assert response.status_code == 200
        snapshot = ""
        for line in response.iter_lines():
            if not line:
                break
            snapshot += line
        assert "stopped" in snapshot and "terminated" in snapshot
    print("PID_GONE_WHEN_STOP_RETURNED", gone(pid))


@pytest.mark.parametrize("neighbor", ["crash", "burn"])
def test_fault_isolation_and_healthy_progress(
    request: pytest.FixtureRequest, tmp_path: Path, neighbor: Scenario
) -> None:
    gates = tmp_path / "audit" / "gates"
    gates.mkdir(parents=True)
    gate = gates / neighbor
    os.mkfifo(gate)
    with (
        gate.open("r+b", buffering=0) as release,
        running_cluster(request, tmp_path, application="agents_isolation_app:app") as cluster,
    ):
        bad = cluster.start(neighbor)[0]
        fault = cluster.wait(bad, lambda value: bool(value.executions.get("step-2")))
        pid = fault.executions["step-2"][0].pid
        assert not gone(pid)
        healthy = cluster.start("normal", count=2)
        for session in healthy:
            progressed = cluster.wait(session, lambda value: bool(value.snapshot.token_times))
            assert progressed.snapshot.token_times[-1] > fault.executions["step-2"][0].entered
            value = cluster.wait(session, lambda value: value.snapshot.state == "completed")
            assert len(value.executions) == 3
            assert all(len(events) == 1 for events in value.executions.values())
            assert all(entry.state == "done" for entry in value.snapshot.ledger.values())
            active = cluster.view(bad)
            assert active.snapshot.state == "running"
            assert active.snapshot.ledger["step-2"].state == "started"
            assert not gone(pid)
        release.write(b"release")
        terminal = "failed" if neighbor == "crash" else "completed"
        result = cluster.wait(bad, lambda value: value.snapshot.state == terminal)
        assert result.snapshot.ledger["step-2"].state == (
            "failed" if neighbor == "crash" else "done"
        )
        assert all(len(events) == 1 for events in result.executions.values())
        assert gone(pid)
        print("HEALTHY_COMPLETED_BEFORE_RELEASE", neighbor, "PID_GONE", gone(pid))


def test_node_kill_resumes_without_repeating_tools(cluster: Cluster) -> None:
    session = cluster.start("hang")[0]
    active = cluster.wait(session, lambda value: bool(value.executions.get("step-2")))
    assert active.snapshot.node == "a"
    assert active.snapshot.ledger["step-1"].state == "done"
    pid = active.executions["step-2"][0].pid
    started = time.monotonic()
    cluster.kill(0)
    result = cluster.wait(session, lambda value: value.snapshot.state == "completed")
    assert result.snapshot.node in ("b", "c")
    assert result.snapshot.ledger["step-1"].state == "done"
    assert result.snapshot.ledger["step-2"].state == "interrupted"
    assert "not re-run" in result.snapshot.ledger["step-2"].result
    assert len(result.executions) == 2
    assert all(len(events) == 1 for events in result.executions.values())
    assert gone(pid)
    assert "interrupted" in result.snapshot.history.lower()
    survivor = cluster.wait(session, lambda value: value.snapshot.state == "completed")
    assert survivor.executions == result.executions
    print(
        "TAKEOVER_MS",
        round((time.monotonic() - started) * 1000, 3),
        "OWNER",
        result.snapshot.node,
        "COUNTS",
        {call: len(events) for call, events in result.executions.items()},
    )


@pytest.mark.parametrize("killed", [0, 2])
def test_new_sessions_complete_after_node_kill(cluster: Cluster, killed: int) -> None:
    session = cluster.start("hang" if killed == 0 else "normal")[0]
    cluster.wait(session, lambda value: bool(value.executions.get("step-2")))
    cluster.kill(killed)
    cluster.wait(session, lambda value: value.snapshot.state == "completed")
    new = cluster.start("normal", count=2, owner="b")
    for session in new:
        result = cluster.wait(
            session, lambda value: value.snapshot.state == "completed", timeout=40
        )
        assert result.snapshot.node != "abc"[killed]
        assert len(result.executions) == 3
        assert all(len(events) == 1 for events in result.executions.values())
