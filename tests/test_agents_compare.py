import os
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest
from pydantic import ValidationError

from examples.agents.compare import (
    ASSETS,
    RESULTS,
    Measurement,
    Report,
    Resources,
    Snapshot,
    Stack,
    View,
    chart,
    repeats,
    resources,
    token_gap,
    write_report,
)
from examples.agents.protocol import Execution


@pytest.mark.parametrize("empty", [False, True])
def test_comparison_writes_benchmark_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, empty: bool
) -> None:
    report = Report.model_validate_json((RESULTS / "comparison.json").read_text())
    if empty:
        report.measurements.clear()
    results, assets = tmp_path / "results" / "agents", tmp_path / "assets"
    assert RESULTS.parts[-3:] == ("benchmarks", "results", "agents")
    assert ASSETS.parts[-2:] == ("benchmarks", "assets")
    monkeypatch.setattr("examples.agents.compare.RESULTS", results)
    monkeypatch.setattr("examples.agents.compare.ASSETS", assets)
    write_report(report)
    assert Report.model_validate_json((results / "comparison.json").read_text()) == report
    assert sorted(path.name for path in assets.iterdir()) == [
        "agents-completion.svg",
        "agents-gaps.svg",
        "agents-repeats.svg",
    ]
    for path in assets.iterdir():
        ElementTree.fromstring(path.read_text())  # noqa: S314


def test_comparison_output_error_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = Report.model_validate_json((RESULTS / "comparison.json").read_text())
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setattr("examples.agents.compare.RESULTS", blocked)
    with pytest.raises(FileExistsError):
        write_report(report)


@pytest.mark.parametrize("running", [True, False])
def test_resource_sampling_worker_killed_during_sample(
    monkeypatch: pytest.MonkeyPatch, running: bool
) -> None:
    monkeypatch.setattr("examples.agents.compare.compose", lambda *_args: "container")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "top":
            raise subprocess.CalledProcessError(1, command, stderr="container is not running")
        output = str(running).lower() if command[1] == "inspect" else '{"Name":"worker"}'
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr("examples.agents.compare.subprocess.run", run)
    if running:
        with pytest.raises(subprocess.CalledProcessError):
            resources("Redis/Celery")
    else:
        result = resources("Redis/Celery")
        assert result.processes == 0 and result.rss_mib == 0
        assert result.docker_stats == [{"Name": "worker"}]


def test_resource_sampling_reads_process_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("examples.agents.compare.compose", lambda *_args: "container")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = "PID\n" + str(os.getpid()) if command[1] == "top" else '{"Name":"worker"}'
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr("examples.agents.compare.subprocess.run", run)
    result = resources("Redis/Celery")
    assert result.processes == 1 and result.rss_mib > 0


def test_comparison_counts_repeats_and_token_gaps() -> None:
    value = View(
        snapshot=Snapshot(id="test", node="a", state="completed", token_times=[1, 1.1, 3]),
        executions={
            "step-1": [Execution(pid=1, entered=1), Execution(pid=2, entered=2)],
            "step-2": [Execution(pid=2, entered=3)],
        },
    )
    assert repeats([value, value]) == 2
    assert token_gap([value]) == pytest.approx(1.9)
    assert repeats([]) == 0
    assert token_gap([]) == 0
    assert (
        token_gap(
            [value.model_copy(update={"snapshot": Snapshot(id="empty", node="a", state="waiting")})]
        )
        == 0
    )


def test_comparison_rejects_invalid_snapshot() -> None:
    with pytest.raises(ValidationError):
        Snapshot.model_validate({"id": "test", "node": "a", "state": "invented"})


def test_comparison_http_failure_and_empty_batch() -> None:
    with (
        httpx.Client(
            base_url="http://test",
            transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
        ) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        Stack("PyBEAM", client).snapshots()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(303) if request.method == "POST" else httpx.Response(200, json=[])

    with (
        httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(RuntimeError, match="batch size"),
    ):
        Stack("PyBEAM", client).start("normal", 12)


def test_comparison_empty_chart_is_valid_and_escapes_titles() -> None:
    report = Report.model_validate(
        {
            "measured_at": "2026-01-01T00:00:00Z",
            "machine": "test",
            "cpu_count": 1,
            "notes": [],
            "measurements": [],
        }
    )
    svg = chart(report, "Time < pause & lookup", "seconds", lambda row: row.max_token_gap_s)
    root = ElementTree.fromstring(svg)  # noqa: S314
    title = root.find("{http://www.w3.org/2000/svg}title")
    assert title is not None and title.text == "Time < pause & lookup"
    assert "prefers-color-scheme:dark" in svg


def test_comparison_chart_shows_measured_rows_and_zero_values() -> None:
    report = Report.model_validate(
        {
            "measured_at": "2026-01-01T00:00:00Z",
            "machine": "test",
            "cpu_count": 1,
            "notes": [],
            "measurements": [],
        }
    )
    report.measurements.append(
        Measurement(
            stack="PyBEAM",
            case="stop",
            baseline_completion_s=[2],
            healthy_completion_s=[3],
            median_relative_to_baseline=1.5,
            max_relative_to_baseline=1.5,
            max_token_gap_s=0,
            healthy_repeats=0,
            fault_repeats=0,
            fault_state="stopped",
            stop_s=0.05,
            kill_to_resume_s=None,
            kill_to_complete_s=None,
            healthy_counts={},
            fault_counts={},
            resources=Resources(processes=3, rss_mib=10),
        )
    )
    svg = chart(report, "Gaps", "seconds", lambda row: row.max_token_gap_s)
    assert "stop · PyBEAM" in svg and ">0.00</text>" in svg
    assert 'width="0.0"' in svg
