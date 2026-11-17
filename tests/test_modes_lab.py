import pytest
from fastapi.testclient import TestClient

from examples.modes_lab.app import (
    FANIN_CONTEXTS,
    LANES,
    LaneRun,
    Outcome,
    Run,
    Span,
    app,
    command,
    execute,
    runs,
)


def span(start: int, end: int, pid: int, thread: int, interpreter: int, gil: bool) -> Span:
    return Span(start=start, end=end, thread=thread, pid=pid, interpreter=interpreter, gil=gil)


def test_modes_lab__fanout_reports_topology_and_speedup() -> None:
    lane = LaneRun(LANES[5])
    lane.status = "done"
    lane.outcome = Outcome(
        count=2,
        single_ns=100_000_000,
        wall_us=100_000,
        spans=[span(0, 100, 7, 1, 0, True), span(0, 100, 7, 2, 0, True)],
    )
    run = Run("key", "fanout", 2, 1_000_000, [lane])

    assert lane.speedup == 2.0
    assert lane.topology == "1 OS process · 2 threads · 1 interpreter · 1 GIL"
    assert [bar.tone for bar in lane.bars(run.scale_ns)] == [0, 1]
    assert run.finished
    assert not lane.kept_gil


def test_modes_lab__free_threaded_spans_have_no_gil() -> None:
    lane = LaneRun(LANES[6])
    lane.outcome = Outcome(spans=[span(0, 1, 7, 1, 0, False), span(0, 1, 7, 2, 0, False)])

    assert lane.topology.endswith("0 GILs")


def test_modes_lab__renders_and_validates_experiments() -> None:
    with TestClient(app) as client:
        page = client.get("/")
        rejected = client.post("/runs", data={"experiment": "bogus"})

    assert page.status_code == 200
    assert "ExecutionMode.ISOLATED" in page.text
    assert rejected.status_code == 422


def test_modes_lab__fanin_passes_a_bounded_context_count() -> None:
    lane = LANES[5]
    run = Run("key", "fanin", 1_000_000, 3_000_000, [LaneRun(lane)])

    assert command(lane, run)[-2:] == ["1000000", str(FANIN_CONTEXTS)]


def test_modes_lab__renders_fanin_results() -> None:
    lane = LaneRun(
        LANES[5], "done", Outcome(count=10_000, wall_us=1_000_000, contexts=8, window=32)
    )
    runs["fanin"] = Run("fanin", "fanin", 10_000, 3_000_000, [lane])
    try:
        with TestClient(app) as client:
            page = client.get("/runs/fanin")
    finally:
        runs.pop("fanin")

    assert page.status_code == 200
    assert "10,000 calls/s" in page.text


async def test_modes_lab__missing_interpreter_fails_the_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYBEAM_LAB_PYTHON_314T", "/nonexistent/python3.14t")
    lane = LaneRun(LANES[2])
    await execute(lane, Run("key", "fanout", 1, 100_000, [lane]))

    assert lane.status == "failed"
    assert lane.detail.startswith("cannot start")
