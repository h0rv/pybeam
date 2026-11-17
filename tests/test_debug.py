import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from pybeam.runtime import attach, debug

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path


def test_registry__lists_live_apps_and_removes_stale_records() -> None:
    live = attach.App("live", "live@127.0.0.1", os.getpid(), "/app/main.py")
    path = attach.announce(live)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    dead = attach.announce(attach.App("dead", "dead@127.0.0.1", 2**22 + 1, "/app/old.py"))
    garbage = attach.registry() / "garbage.json"
    garbage.write_text(json.dumps({"name": 1}))
    assert attach.running() == [live]
    assert not dead.exists()
    assert not garbage.exists()


def test_node_name__is_a_safe_short_name() -> None:
    assert attach.node_name(Path("/srv/my app.py")) == "my_app-" + str(os.getpid())


def test_evaluate__returns_repr_runs_statements_and_reports_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(debug, "_namespace", {})
    monkeypatch.setattr(debug, "entry_globals", lambda: {"answer": 42})
    assert debug.evaluate("answer + 1") == "43"
    assert debug.evaluate("value = answer * 2") == ""
    assert debug.evaluate("value") == "84"
    assert "ZeroDivisionError" in debug.evaluate("1 / 0")


async def test_snapshot__shows_entry_loop_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def parked() -> None:
        await asyncio.sleep(10)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(debug, "entry_loop", lambda: loop)
    task = asyncio.create_task(parked(), name="parked-task")
    await asyncio.sleep(0)
    try:
        text = await asyncio.to_thread(debug.snapshot, "")
    finally:
        task.cancel()
    assert "parked-task" in text
    assert "parked()" in text


def test_cli_ps__lists_nothing_without_debug_apps(runtime_dir: Path) -> None:
    environment = dict(os.environ)
    environment["XDG_RUNTIME_DIR"] = str(runtime_dir)
    result = subprocess.run(
        ["uv", "run", "--project", str(ROOT), "pybeam", "ps"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "pybeam run --debug" in result.stdout
