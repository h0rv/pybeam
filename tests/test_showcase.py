import asyncio
import subprocess
import sys
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from benchmarks import runner
from benchmarks.contract import Budget, Config, Identity, Outcome
from benchmarks.ownership import Control, identity, resolve
from benchmarks.runner import measurement_lock, trial
from benchmarks.tests.test_runner import wait_for
from examples.showcase.app import create_app
from examples.showcase.catalog import ROOT, catalog
from examples.showcase.jobs import Job, Jobs

BASE = "http://127.0.0.1:38921"


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "evil.example"},
        {"origin": "http://evil.example"},
        {"sec-fetch-site": "cross-site"},
    ],
)
def test_showcase__rejects_rebinding_and_cross_site(headers: dict[str, str]) -> None:
    with TestClient(create_app(), base_url=BASE) as client:
        assert client.get("/", headers=headers).status_code == 403


def test_showcase__help_contains_actual_run_command() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "examples.showcase", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    help_text = " ".join(result.stdout.split())
    assert "uv run --extra showcase python -m examples.showcase" in help_text
    assert "no public bind option" in help_text


def test_showcase__fixed_commands_and_sources_only() -> None:
    with TestClient(create_app(), base_url=BASE) as client:
        assert client.get("/").status_code == 200
        assert client.get("/source/smoke").text.startswith("import")
        assert client.get("/source/etc/passwd").status_code == 404
        assert client.post("/start/smoke").status_code == 403
        headers = {"origin": BASE}
        assert client.post("/start/discord", headers=headers).status_code == 409
        assert client.post("/start/sh", headers=headers).status_code == 404
        assert client.post("/start/smoke?argv=whoami", headers=headers).status_code == 400
        assert (
            client.post("/start/smoke", headers=headers, json={"command": "whoami"}).status_code
            == 400
        )
        assert client.get("/artifact/not-owned").status_code == 404


def test_showcase__trusted_catalog_rejects_escaping_launcher(tmp_path: Path) -> None:
    root = tmp_path / "trusted"
    (root / "examples").mkdir(parents=True)
    outside = tmp_path / "outside.escript"
    outside.write_text("not executable")
    (root / "examples/run_asgi.escript").symlink_to(outside)
    native = next(card for card in catalog(root) if card.key == "fastapi")
    assert native.disabled == "Trusted launcher escapes its configured source root."
    default = next(card for card in catalog() if card.key == "fastapi")
    assert default.launcher is None
    assert default.disabled is not None


def test_showcase__logs_escaped_and_bounded(tmp_path: Path) -> None:
    card = catalog()[0]
    job = Job("test", card, tmp_path)
    (tmp_path / "runner.log").write_text("x" * 20000 + "<script>alert(1)</script>")
    assert len(job.logs()) <= 16384
    with TestClient(create_app(), base_url=BASE) as client:
        # Template escapes text returned from the log reader, including markup.
        from fastapi.templating import Jinja2Templates

        template = Jinja2Templates(directory=ROOT / "examples/showcase").env.get_template(
            "jobs.html"
        )
        rendered = template.render(jobs=(job,))
        assert "&lt;script&gt;" in rendered
        assert "<script>alert" not in rendered
        assert client.get("/").headers["content-security-policy"].startswith("default-src 'self'")


async def test_showcase__queue_is_bounded(tmp_path: Path) -> None:
    async with asyncio.TaskGroup() as group:
        manager = Jobs(group, tmp_path)
        card = catalog()[0]
        for _ in range(4):
            manager.start(card)
        with pytest.raises(ValueError, match="Session limit"):
            manager.start(card)
        await manager.close()
    assert all(job.status == "stopped" for job in manager.items.values())


@pytest.mark.parametrize("mode", ["stop", "owner_exit"])
def test_guardian__control_reaps_owned_child(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "LOAD_LOCK", tmp_path / "load.lock")
    config = Config(
        python_source=str(ROOT),
        native_source=str(ROOT),
        repeats=1,
        budget=Budget(wall_seconds=5.0),
        adapter_command=(sys.executable, "-m", "benchmarks.tests.probes", "child_wait"),
    )
    stop = tmp_path / "stop.request"
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    control = Control(owner=identity(psutil.Process(owner.pid)), stop_file=str(stop))

    async def perform() -> Outcome:
        with measurement_lock() as descriptor:
            async with asyncio.TaskGroup() as group:
                task = group.create_task(
                    asyncio.to_thread(trial, config, tmp_path / "trial", descriptor, control)
                )
                await asyncio.to_thread(wait_for, tmp_path / "trial/test-child.json")
                if mode == "stop":
                    stop.touch()
                else:
                    owner.kill()
                    await asyncio.to_thread(owner.wait, 3)
        return task.result()

    try:
        outcome = asyncio.run(perform())
    finally:
        if owner.poll() is None:
            owner.terminate()
        owner.wait(timeout=3)
    child = Identity.model_validate_json((tmp_path / "trial/test-child.json").read_text())
    assert outcome.reason == (
        "Stop requested; side effects are not rolled back"
        if mode == "stop"
        else "Control owner exited"
    )
    assert not outcome.cleanup_survivors
    assert resolve(child) is None
