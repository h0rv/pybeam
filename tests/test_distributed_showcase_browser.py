import select
import socket
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import APIResponse, Route, expect, sync_playwright

from examples.showcase.catalog import ROOT
from tests.fixtures.showcase_boundary import PORT

BASE = "http://127.0.0.1:" + str(PORT)
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
pytestmark = pytest.mark.browser


@pytest.fixture
def controlled_server(tmp_path: Path) -> Generator[str]:
    with socket.socket() as listener, (tmp_path / "server.log").open("wb") as log:
        # One fixed private port. A conflict is an honest failure, never a retry on another port.
        listener.bind(("127.0.0.1", PORT))
        listener.listen(32)
        with subprocess.Popen(
            [sys.executable, "-m", "tests.fixtures.showcase_boundary", str(listener.fileno())],
            cwd=ROOT,
            pass_fds=(listener.fileno(),),
            stdout=subprocess.PIPE,
            stderr=log,
        ) as process:
            try:
                assert process.stdout is not None
                readable, _, _ = select.select([process.stdout], [], [], 10)
                assert readable, "Controlled UI server failed startup; inspect server.log"
                assert process.stdout.readline() == b"READY\n"
                yield BASE
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def test_controlled_browser__two_contexts_sse_post_races_and_sanitized_download(
    controlled_server: str,
) -> None:
    with sync_playwright() as playwright:
        assert CHROME.is_file(), "Requested Google Chrome prerequisite is missing"
        browser = playwright.chromium.launch(
            executable_path=str(CHROME), headless=True, chromium_sandbox=True
        )
        try:
            first = browser.new_context()
            second = browser.new_context()
            a = first.new_page()
            b = second.new_page()
            errors: list[str] = []
            a.on("pageerror", lambda error: errors.append(str(error)))
            b.on("pageerror", lambda error: errors.append(str(error)))
            a.goto(controlled_server)
            b.goto(controlled_server)
            held: list[tuple[Route, APIResponse]] = []

            def delay(route: Route) -> None:
                held.append((route, route.fetch()))

            a.route("**/start/distributed", delay)
            a.get_by_role("button", name="Run Controlled boundary:").click()
            expect(a.locator('[data-key="distributed"]')).to_have_attribute(
                "data-status", "running"
            )
            expect(b.locator('[data-key="distributed"]')).to_have_attribute(
                "data-status", "running"
            )
            assert "No birth receipt observed" in a.locator("#jobs").inner_text()
            assert len(held) == 1
            # Actual POST was accepted. Its HTTP acknowledgement is still held in the browser.
            with httpx.Client(base_url=controlled_server) as client:
                assert client.get("/__test__/subscribers").text == '{"count":2}'
                assert client.get("/__test__/finish").status_code == 204
            expect(a.locator('[data-key="distributed"]')).to_have_attribute(
                "data-status", "finished"
            )
            expect(b.locator('[data-key="distributed"]')).to_have_attribute(
                "data-status", "finished"
            )
            revision = a.locator("#jobs").get_attribute("data-revision")
            route, response = held.pop()
            assert response.status == 204
            route.fulfill(response=response)
            expect(a.locator('[data-key="distributed"]')).to_have_attribute(
                "data-status", "finished"
            )
            assert a.locator("#jobs").get_attribute("data-revision") == revision
            expect(a.get_by_text("Task result 42.", exact=False)).to_have_count(2)
            expect(b.get_by_text("B rejected the stale ACK", exact=False)).to_be_visible()
            assert "Controlled boundary" in a.locator("#jobs").inner_text()
            job_id = a.locator('[data-key="distributed"]').get_attribute("id")
            assert job_id is not None
            key = job_id.removeprefix("job-")
            with httpx.Client(base_url=controlled_server) as client:
                for _ in range(2):
                    reply = client.post(
                        "/cancel/" + key,
                        headers={"origin": controlled_server, "hx-request": "true"},
                    )
                    assert reply.status_code == 204
                    assert not reply.content
                proof = client.get("/artifact/" + key)
                assert proof.status_code == 200
                assert "SECRET" not in proof.text
                assert "token" not in proof.text
                assert "directory" not in proof.text
                assert key in proof.headers["content-disposition"]
                assert client.get("/artifact/not-owned").status_code == 404
            # Disconnect and reconnect force an authoritative read, not replay of old HTML.
            b.goto("about:blank")
            b.goto(controlled_server)
            expect(b.locator('[data-key="distributed"]')).to_have_attribute(
                "data-status", "finished"
            )
            assert not errors
            first.close()
            second.close()
            with httpx.Client(base_url=controlled_server) as client:
                assert client.get("/__test__/subscribers").text == '{"count":0}'
        finally:
            browser.close()


def test_controlled_browser__cancel_ack_cannot_revive_and_full_queue(
    controlled_server: str,
) -> None:
    with sync_playwright() as playwright:
        assert CHROME.is_file(), "Requested Google Chrome prerequisite is missing"
        browser = playwright.chromium.launch(
            executable_path=str(CHROME), headless=True, chromium_sandbox=True
        )
        try:
            page = browser.new_page()
            page.goto(controlled_server)
            page.get_by_role("button", name="Run Controlled boundary:").click()
            expect(page.locator('[data-key="distributed"]')).to_have_attribute(
                "data-status", "running"
            )
            held: list[tuple[Route, APIResponse]] = []

            def delay(route: Route) -> None:
                held.append((route, route.fetch()))

            page.route("**/cancel/*", delay)
            page.get_by_role("button", name="Cancel Controlled boundary:").click()
            expect(page.locator('[data-key="distributed"]')).to_have_attribute(
                "data-status", "stopping"
            )
            with httpx.Client(base_url=controlled_server) as client:
                assert client.get("/__test__/finish").status_code == 204
            expect(page.locator('[data-key="distributed"]')).to_have_attribute(
                "data-status", "stopped"
            )
            route, response = held.pop()
            assert response.status == 204
            route.fulfill(response=response)
            expect(page.locator('[data-key="distributed"]')).to_have_attribute(
                "data-status", "stopped"
            )
            with httpx.Client(base_url=controlled_server) as client:
                for _ in range(4):
                    assert (
                        client.post(
                            "/start/distributed", headers={"origin": controlled_server}
                        ).status_code
                        == 303
                    )
                assert (
                    client.post(
                        "/start/distributed", headers={"origin": controlled_server}
                    ).status_code
                    == 409
                )
                panel = client.get("/jobs").text
                assert "queued" in panel
            expect(page.locator('[data-key="distributed"]')).to_have_count(5)
            # Cancel every remaining queued/running item using the public owned routes.
            for element in page.locator('[data-key="distributed"]').all():
                key = element.get_attribute("id")
                assert key is not None
                with httpx.Client(base_url=controlled_server) as client:
                    client.post(
                        "/cancel/" + key.removeprefix("job-"), headers={"origin": controlled_server}
                    )
            with httpx.Client(base_url=controlled_server) as client:
                client.get("/__test__/finish")
            expect(page.locator('[data-status="stopped"]')).to_have_count(5)
        finally:
            browser.close()
