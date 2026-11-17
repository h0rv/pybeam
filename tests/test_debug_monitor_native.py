import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pybeam.runtime.assets import discover_assets
from pybeam.runtime.attach import connect, running
from pybeam.runtime.config import load_config
from pybeam.runtime.inspector import REPORT
from tests.native.runtime import ROOT


@pytest.mark.runtime
def test_monitor__reads_real_actor_task_and_queue_state_without_changing_app(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and a built native runtime")
    profile: object = request.config.getoption("hornbeam_runtime")
    assert isinstance(profile, str)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(Path(profile).resolve()) + '"\n'
    )
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    with (tmp_path / "app.log").open("w") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from pybeam.runtime.cli import main; main()",
                "run",
                "--debug",
                str(ROOT / "tests/fixtures/debug_monitor.py"),
            ],
            cwd=tmp_path,
            env=environment,
            stdout=output,
            stderr=output,
        )
        try:
            deadline = time.monotonic() + 30
            while not (tmp_path / "monitor.ready").exists():
                assert process.poll() is None, (tmp_path / "app.log").read_text()
                assert time.monotonic() < deadline, (tmp_path / "app.log").read_text()
                time.sleep(0.02)
            apps = running()
            assert len(apps) == 1
            assets = discover_assets(load_config(tmp_path))
            with connect(apps[0], assets) as connection:
                report = REPORT.decode(connection.request("inspect", "", timeout=5))
                assert not report.unavailable
                rows = {row.kind: row for row in report.resources if row.kind != "tasks"}
                supervisors = {
                    {stat.name: stat.value for stat in row.stats}["Capacity"]: row
                    for row in report.resources
                    if row.kind == "tasks"
                }
                actor = rows["actor"]
                assert actor.name == "Counter-0"
                assert {stat.name: stat.value for stat in actor.stats}["Admitted calls"] == "1"
                assert {stat.name: stat.value for stat in supervisors["3"].stats}[
                    "Free slots"
                ] == "2"
                deadline = time.monotonic() + 10
                while True:
                    report = REPORT.decode(connection.request("inspect", "", timeout=5))
                    retained = next(
                        row
                        for row in report.resources
                        if row.kind == "tasks"
                        and any(stat.name == "Capacity" and stat.value == "1" for stat in row.stats)
                    )
                    stats = {stat.name: stat.value for stat in retained.stats}
                    if stats["Admitted calls"] == "0":
                        break
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
                assert stats["Owned results"] == "1"
                assert stats["Free slots"] == "0"
                assert retained.state == "full"
                byte_stats = {stat.name: stat.value for stat in supervisors["2"].stats}
                assert byte_stats["Free slots"] == "1"
                assert byte_stats["Encoded bytes"] == byte_stats["Byte limit"] == "1"
                assert supervisors["2"].state == "full"
                assert {stat.name: stat.value for stat in rows["queue"].stats} == {
                    "Queue depth": "1",
                    "Active leases": "1",
                    "Capacity": "4",
                }
                assert "counter-wait" in connection.request("snapshot", "", timeout=5)
                assert connection.request("evaluate", "6 * 7", timeout=5) == "42"
            assert process.poll() is None
            with connect(apps[0], assets) as connection:
                assert REPORT.decode(connection.request("inspect", "", timeout=5)).resources
        finally:
            process.terminate()
            process.wait(timeout=20)
