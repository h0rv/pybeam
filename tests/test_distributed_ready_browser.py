import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import APIResponse, Route, expect, sync_playwright

from examples.distributed.models import WalkthroughProof
from examples.distributed.profile import verify_prepared
from examples.showcase.catalog import ROOT
from pybeam.runtime.ownership import resolve
from tests.fixtures.distributed_native_server import Audit

PORT = 38927
BASE = "http://127.0.0.1:" + str(PORT)
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
pytestmark = [pytest.mark.browser, pytest.mark.runtime]


def wait_audit(path: Path, *, subscribers: int) -> Audit:
    deadline = time.monotonic() + 5
    while True:
        if path.is_file():
            audit = Audit.model_validate_json(path.read_bytes())
            if audit.subscribers == subscribers:
                return audit
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_native_browser__finite_run_cancel_sse_and_owned_sigterm(tmp_path: Path) -> None:
    verify_prepared()
    assert CHROME.is_file()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", PORT))
    audit_path = tmp_path / "audit.json"
    with (tmp_path / "server.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.fixtures.distributed_native_server",
                str(PORT),
                str(audit_path),
            ],
            cwd=ROOT,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 15
            with httpx.Client(base_url=BASE, timeout=1) as client:
                while True:
                    assert process.poll() is None, (tmp_path / "server.log").read_text()
                    try:
                        if client.get("/").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=str(CHROME), headless=True, chromium_sandbox=True
                )
                try:
                    first = browser.new_context()
                    second = browser.new_context()
                    a, b = first.new_page(), second.new_page()
                    errors: list[str] = []
                    a.on("pageerror", lambda error: errors.append(str(error)))
                    b.on("pageerror", lambda error: errors.append(str(error)))
                    a.goto(BASE)
                    b.goto(BASE)
                    wait_audit(audit_path, subscribers=2)
                    held: list[tuple[Route, APIResponse]] = []

                    def delay(route: Route) -> None:
                        held.append((route, route.fetch()))

                    a.route("**/start/distributed", delay)
                    a.get_by_role("button", name=re.compile("^Run Scripted two-node")).click()
                    job = a.locator('[data-key="distributed"]').first
                    expect(job).to_have_attribute("data-status", "finished", timeout=60000)
                    expect(b.locator('[data-key="distributed"]').first).to_have_attribute(
                        "data-status", "finished"
                    )
                    revision = a.locator("#jobs").get_attribute("data-revision")
                    route, response = held.pop()
                    assert response.status == 204
                    route.fulfill(response=response)
                    assert a.locator("#jobs").get_attribute("data-revision") == revision
                    expect(a.get_by_text("Task result 42.", exact=False)).to_have_count(2)
                    expect(b.get_by_text("B rejected the stale ACK", exact=False)).to_be_attached()
                    expect(a.locator("#current-run .result")).to_be_in_viewport()
                    expect(
                        a.get_by_role("link", name="Download walkthrough proof")
                    ).to_be_in_viewport()
                    key = job.get_attribute("id")
                    assert key is not None
                    with httpx.Client(base_url=BASE) as client:
                        download = client.get("/artifact/" + key.removeprefix("job-"))
                    assert download.status_code == 200
                    proof = WalkthroughProof.model_validate_json(download.content)
                    (tmp_path / "download.json").write_bytes(download.content)
                    for forbidden in ("cookie", "token", "payload", "directory", str(ROOT)):
                        assert forbidden not in download.text
                    audit = Audit.model_validate_json(audit_path.read_bytes())
                    run = audit.jobs[0]
                    assert run.proof == proof and run.outcome is not None
                    assert run.outcome.forced_cleanup_count == 0
                    assert run.registry is not None
                    assert all(resolve(birth) is None for birth in run.registry.owned)
                    a.screenshot(path=str(tmp_path / "finished.png"), full_page=True)
                    a.unroute("**/start/distributed", delay)
                    a.get_by_role("button", name=re.compile("^Run Scripted two-node")).click()
                    live = a.locator('[data-key="distributed"]').first
                    expect(live.locator('[data-node="a"]')).to_contain_text(
                        "pybeam_a_", timeout=30000
                    )
                    a.route("**/cancel/*", delay)
                    live.get_by_role("button", name=re.compile("^Cancel")).click()
                    expect(live).to_have_attribute("data-status", "stopped", timeout=30000)
                    expect(b.locator('[data-key="distributed"]').first).to_have_attribute(
                        "data-status", "stopped"
                    )
                    route, response = held.pop()
                    assert response.status == 204
                    route.fulfill(response=response)
                    expect(live).to_have_attribute("data-status", "stopped")
                    audit = Audit.model_validate_json(audit_path.read_bytes())
                    cancelled = audit.jobs[-1]
                    assert cancelled.outcome is not None and cancelled.registry is not None
                    assert not cancelled.outcome.cleanup_survivors
                    assert all(resolve(birth) is None for birth in cancelled.registry.owned)
                    assert cancelled.outcome.forced_cleanup_count == 0
                    assert not errors
                    # Keep both SSE connections open during SIGTERM and the real lifespan drain.
                    process.terminate()
                    process.wait(timeout=20)
                    audit = wait_audit(audit_path, subscribers=0)
                    assert audit.close_calls == 1
                    assert all(job.status in {"finished", "stopped"} for job in audit.jobs)
                    first.close()
                    second.close()
                finally:
                    browser.close()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
