import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest
from playwright.sync_api import Locator, expect, sync_playwright

from benchmarks.ownership import identity, resolve
from benchmarks.runner import TrialSummary, measurement_lock
from examples.showcase.catalog import ROOT
from pybeam.codec import Json

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def finished(job: Locator) -> None:
    expect(job).not_to_have_attribute(
        "data-status", re.compile("queued|running|stopping"), timeout=20000
    )
    assert job.get_attribute("data-status") == "finished", job.inner_text()


@pytest.mark.browser
@pytest.mark.benchmark
def test_showcase__actual_chrome_controls_and_evidence(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    if request.config.getoption("run_browser") is not True:
        pytest.skip("Requires explicit --run-browser; this local UI test needs no native server")
    if not CHROME.is_file():
        pytest.fail("--run-browser requested, but Google Chrome is not installed: " + str(CHROME))
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    BASE = "http://127.0.0.1:" + str(port)
    trusted_native = os.environ.get("PYBEAM_BENCHMARK_NATIVE_ROOT")
    argv = [sys.executable, "-m", "examples.showcase", "--port", str(port)]
    profiles = ("python", "erlang")
    if trusted_native is not None:
        argv.extend(("--trusted-native-root", trusted_native))
        profiles = (*profiles, "pybeam")
    with (tmp_path / "server.log").open("wb") as log:
        server = subprocess.Popen(
            argv,
            cwd=ROOT,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                if server.poll() is not None:
                    raise RuntimeError((tmp_path / "server.log").read_text())
                try:
                    if httpx.get(BASE, timeout=0.2).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("UI failed to start")
                time.sleep(0.05)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(CHROME), headless=True)
                page = browser.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: (
                        errors.append(message.text) if message.type == "error" else None
                    ),
                )
                page.goto(BASE)
                assert page.title() == "PyBeam showcase"
                page.goto(BASE + "/?scenario=discord")
                assert page.locator("#card-discord button").is_disabled()
                page.goto(BASE + "/?scenario=stream")
                expect(page.locator("#scenario-details")).to_contain_text("0b4dd8")
                page.goto(BASE + "/?scenario=smoke")
                page.get_by_text("Scenario details", exact=True).click()
                page.get_by_role("link", name="View example code").click()
                assert "import" in page.locator("body").inner_text()
                page.goto(BASE + "/?scenario=smoke")
                page.get_by_role("button", name="Start Python benchmark smoke", exact=True).click()
                smoke = page.locator('[data-key="smoke"]')
                finished(smoke)
                assert "100" in smoke.locator("table").inner_text()
                assert smoke.locator("svg rect").count() == 1
                href = smoke.get_by_role("link", name="Download actual trials.jsonl").get_attribute(
                    "href"
                )
                assert href is not None
                evidence = page.request.get(BASE + href).text()
                result = Json(TrialSummary).decode(evidence.strip())
                assert result.outcome.status == "passed"
                assert result.completed == 100
                assert result.throughput_per_second is not None
                assert result.throughput_per_second > 0
                print(
                    "Chrome actual Python completions:",
                    result.completed,
                    "events/s:",
                    result.throughput_per_second,
                )
                page.goto(BASE + "/?scenario=hold")
                page.get_by_role(
                    "button", name="Start Owned cancellation probe", exact=True
                ).click()
                hold = page.locator('[data-key="hold"]')
                expect(hold.locator("pre")).to_contain_text(
                    "Guardian owns child PID", timeout=15000
                )
                hold.get_by_role(
                    "button", name="Cancel Owned cancellation probe", exact=True
                ).click()
                hold = page.locator('[data-key="hold"]')
                hold.locator('[role="status"]').filter(has_text="stopped").wait_for(timeout=15000)
                href = hold.get_by_role("link", name="Download actual trials.jsonl").get_attribute(
                    "href"
                )
                assert href is not None
                stopped = Json(TrialSummary).decode(page.request.get(BASE + href).text().strip())
                assert not stopped.outcome.cleanup_survivors
                assert stopped.outcome.forced_cleanup_count >= 1
                assert (
                    stopped.outcome.reason is not None
                    and "Stop requested" in stopped.outcome.reason
                )
                print(
                    "Chrome cancellation: forced cleanup",
                    stopped.outcome.forced_cleanup_count,
                    "survivors",
                    len(stopped.outcome.cleanup_survivors),
                )
                assert (
                    page.request.post(BASE + "/start/discord", headers={"origin": BASE}).status
                    == 409
                )
                assert page.request.post(BASE + "/start/sh", headers={"origin": BASE}).status == 404
                assert (
                    page.request.post(
                        BASE + "/start/smoke", headers={"origin": BASE}, data={"argv": "whoami"}
                    ).status
                    == 400
                )
                page.get_by_text("Scenario details", exact=True).click()
                page.get_by_role("button", name="Run all available benchmarks", exact=True).click()
                for name in profiles:
                    page.goto(BASE + "/?scenario=benchmark-" + name)
                    profile = page.locator('[data-key="benchmark-' + name + '"]')
                    finished(profile)
                    assert "100" in profile.locator("table").inner_text()
                comparison = page.get_by_role("table", name="Actual scalar results", exact=False)
                assert comparison.locator("tbody tr").count() == 1
                assert errors == []
                print(
                    "Chrome rendered actual scalar profiles:",
                    profiles,
                    "Page and console errors:",
                    errors,
                )
                page.goto(BASE + "/?scenario=hold")
                page.get_by_role(
                    "button", name="Start Owned cancellation probe", exact=True
                ).click()
                failed_hold = page.locator('[data-key="hold"]').first
                expect(failed_hold.locator("pre")).to_contain_text(
                    "Guardian owns child PID", timeout=15000
                )
                descendants = psutil.Process(server.pid).children(recursive=True)
                captured = tuple(identity(child) for child in descendants)
                guardian = next(
                    identity(child)
                    for child in descendants
                    if "benchmarks.guardian" in child.cmdline()
                )
                target = resolve(guardian)
                assert target is not None
                target.kill()
                failed_hold.locator('[role="status"]').filter(has_text="failed").wait_for(
                    timeout=15000
                )
                href = failed_hold.get_by_role(
                    "link", name="Download actual trials.jsonl"
                ).get_attribute("href")
                assert href is not None
                failure = Json(TrialSummary).decode(page.request.get(BASE + href).text().strip())
                assert failure.outcome.cleanup_state == "confirmed"
                assert not failure.outcome.cleanup_survivors
                assert all(resolve(value) is None for value in captured)
                with measurement_lock():
                    pass
                print(
                    "Chrome guardian death: registered processes gone, cleanup confirmed, lock reusable"
                )
                page.get_by_role(
                    "button", name="Start Owned cancellation probe", exact=True
                ).click()
                expect(page.locator('[data-key="hold"]').first.locator("pre")).to_contain_text(
                    "Guardian owns child PID", timeout=15000
                )
                shutdown_owned = tuple(
                    identity(child) for child in psutil.Process(server.pid).children(recursive=True)
                )
                assert errors == []
                browser.close()
        finally:
            server.terminate()
            server.wait(timeout=40)
        assert all(resolve(value) is None for value in shutdown_owned)
        print("Application shutdown: all captured owned identities gone")
