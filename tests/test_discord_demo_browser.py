import asyncio
import socket
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.async_api import Page, Route, async_playwright, expect

from examples.discord_demo.main import Resources

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
pytestmark = [pytest.mark.runtime, pytest.mark.browser]


@dataclass(frozen=True, slots=True)
class Miniapp:
    url: str
    log: Path
    process: asyncio.subprocess.Process


@pytest.fixture
async def miniapp(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncGenerator[Miniapp]:
    if not request.config.getoption("run_runtime") or not request.config.getoption("run_browser"):
        pytest.fail("Native browser acceptance requires --run-browser --run-runtime")
    if not await asyncio.to_thread(CHROME.is_file):
        pytest.fail("--run-browser requested, but Google Chrome is not installed: " + str(CHROME))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = "http://127.0.0.1:" + str(port)
    log = tmp_path / "discord-miniapp.log"
    with log.open("wb") as output:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "examples.discord_demo.main",
            "--port",
            str(port),
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            async with asyncio.timeout(15), httpx.AsyncClient(timeout=0.5) as client:
                while True:
                    assert process.returncode is None, log.read_text()
                    try:
                        response = await client.get(url + "/health")
                    except httpx.TransportError:
                        await asyncio.sleep(0.05)
                        continue
                    assert response.status_code == 200, log.read_text()
                    break
            yield Miniapp(url, log, process)
        finally:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                async with asyncio.timeout(10):
                    await process.wait()
            except TimeoutError:
                process.kill()
                async with asyncio.timeout(5):
                    await process.wait()
                pytest.fail("Managed miniapp shutdown timed out: " + log.read_text())
    text = log.read_text()
    assert process.returncode == 143, text
    assert "Application shutdown complete" in text, text
    assert "Application shutdown failed" not in text, text
    assert "Traceback" not in text or "Slow SSE peer exceeded" in text, text
    assert "PyBeam cleanup failed" not in text, text
    assert "cleanup was not acknowledged" not in text, text


async def test_miniapp__shutdown_drains_live_native_stream(miniapp: Miniapp) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(miniapp.url + "/rooms/general")
            await expect(page.locator("#inspector")).to_be_visible()
            counts = await resource_counts(page, miniapp.url)
            assert counts.streams == counts.subscriptions == 1
            miniapp.process.terminate()
            async with asyncio.timeout(8):
                await miniapp.process.wait()
            assert miniapp.process.returncode == 143, miniapp.log.read_text()
            await context.close()
        finally:
            await browser.close()


async def test_miniapp__nonreading_output_peer_releases_native_subscription(
    miniapp: Miniapp,
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(miniapp.url + "/rooms/general")
            await expect(page.locator("#inspector")).to_be_visible()
            await join(page)
            baseline = await resource_counts(page, miniapp.url)
            cookie = next(
                item["value"] for item in await context.cookies() if item["name"] == "discord"
            )
            csrf = await page.locator('input[name="csrf"][form="compose"]').input_value()
            generation = await page.locator("#inspector").get_attribute("data-generation")
            peer = socket.socket()
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            peer.setblocking(False)
            port = int(miniapp.url.rsplit(":", 1)[1])
            await asyncio.get_running_loop().sock_connect(peer, ("127.0.0.1", port))
            reader, writer = await asyncio.open_connection(sock=peer, limit=1024)
            try:
                request = (
                    "GET /rooms/general/events HTTP/1.1\r\nHost: 127.0.0.1:"
                    + str(port)
                    + "\r\nCookie: discord="
                    + cookie
                    + "\r\n\r\n"
                )
                writer.write(request.encode("ascii"))
                await writer.drain()
                async with asyncio.timeout(5):
                    assert b"200 OK" in await reader.readuntil(b"\r\n\r\n")
                active = await resource_counts(page, miniapp.url)
                assert active.subscriptions == baseline.subscriptions + 1
                for _ in range(100):
                    response = await page.request.post(
                        miniapp.url + "/rooms/general/actions",
                        form={
                            "action": "send",
                            "generation": generation or "0",
                            "csrf": csrf,
                            "text": "🦀" * 256,
                        },
                        headers={"origin": miniapp.url, "hx-request": "true"},
                    )
                    assert "Confirmed send" in await response.text()
                async with asyncio.timeout(12):
                    while True:
                        final = await resource_counts(page, miniapp.url)
                        if final.subscriptions == final.streams == 1:
                            break
                        await asyncio.sleep(0.05)
                assert final.live_contexts == baseline.live_contexts
                assert "Slow SSE peer exceeded" in miniapp.log.read_text()
                async with asyncio.timeout(5):
                    while await reader.read(65536):
                        pass
                print("Nonreading peer retired:", final.model_dump_json())
            finally:
                writer.close()
                await writer.wait_closed()
            await context.close()
        finally:
            await browser.close()


async def join(page: Page) -> None:
    await page.get_by_role("button", name="Join / rejoin").click()
    await expect(page.locator("#membership")).to_have_text("Joined this generation.")


async def send(page: Page, text: str) -> None:
    await page.get_by_label("Message", exact=True).fill(text)
    await page.get_by_role("button", name="Send message").click()


async def resource_counts(page: Page, url: str) -> Resources:
    response = await page.request.get(url + "/resources")
    assert response.ok
    return Resources.model_validate_json(await response.body())


async def test_miniapp__two_chrome_sessions_push_ack_race_and_native_restart(
    miniapp: Miniapp,
) -> None:
    errors: list[str] = []
    network_errors: list[str] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        first_context = await browser.new_context()
        second_context = await browser.new_context()
        release_ack = asyncio.Event()
        try:
            first = await first_context.new_page()
            second = await second_context.new_page()

            async def delayed_extension(route: Route) -> None:
                await asyncio.sleep(0.2)
                await route.continue_()

            await second.route("**/static/sse.js", delayed_extension)
            for page in (first, second):
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("requestfailed", lambda request: network_errors.append(request.url))
            await first.goto(miniapp.url + "/rooms/general")
            await expect(first.locator("#inspector")).to_be_visible()
            await second.goto(miniapp.url + "/rooms/general")
            await expect(second.locator("#inspector")).to_be_visible()
            initial = await resource_counts(first, miniapp.url)
            assert initial.streams == initial.subscriptions == 2
            assert initial.operations == 0
            await asyncio.sleep(2.2)
            assert not errors
            assert not network_errors
            foreign_csrf = await second.locator('input[name="csrf"][form="compose"]').input_value()
            foreign = await second.request.post(
                miniapp.url + "/rooms/general/actions",
                form={"action": "crash", "generation": "0", "csrf": foreign_csrf},
                headers={"origin": miniapp.url, "hx-request": "true"},
            )
            assert foreign.status == 403
            assert (await second.request.get(miniapp.url + "/resources")).status == 403
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(join(first))
                tasks.create_task(join(second))
            for page in (first, second):
                await expect(page.locator("#members li")).to_have_count(2)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(send(first, "first concurrent message"))
                tasks.create_task(send(second, "second concurrent message"))
            for page in (first, second):
                await expect(page.locator("#messages")).to_contain_text("first concurrent message")
                await expect(page.locator("#messages")).to_contain_text("second concurrent message")

            actual_post_finished = asyncio.Event()

            async def delay_real_ack(route: Route) -> None:
                response = await route.fetch()
                actual_post_finished.set()
                await release_ack.wait()
                await route.fulfill(response=response)

            await first.route("**/actions", delay_real_ack)
            await send(first, "delayed acknowledgement")
            async with asyncio.timeout(5):
                await actual_post_finished.wait()
            await send(second, "later message must survive old acknowledgement")
            await expect(first.locator("#messages")).to_contain_text("later message must survive")
            release_ack.set()
            await expect(first.locator("#ack")).to_contain_text("Confirmed send")
            await expect(first.locator("#messages")).to_contain_text("later message must survive")
            await first.unroute("**/actions", delay_real_ack)

            old_pid = await first.locator("#inspector").get_attribute("data-pid")
            old_generation = await first.locator("#inspector").get_attribute("data-generation")
            csrf = await first.locator('input[name="csrf"][form="compose"]').input_value()
            other = await second_context.new_page()
            await other.goto(miniapp.url + "/rooms/python")
            await join(other)
            other_pid = await other.locator("#inspector").get_attribute("data-pid")
            await first.get_by_role("button", name="Crash this native room").click()
            await expect(first.locator("#inspector")).not_to_have_attribute(
                "data-pid", old_pid or ""
            )
            for page in (first, second):
                await expect(page.locator("#members li")).to_have_text(["No native memberships."])
                await expect(page.locator("#messages")).to_contain_text("No messages")
                await expect(page.locator("#membership")).to_contain_text("Explicit join required")
            fresh_pid = await first.locator("#inspector").get_attribute("data-pid")
            stale = await first.request.post(
                miniapp.url + "/rooms/general/actions",
                form={
                    "action": "crash",
                    "generation": old_generation or "0",
                    "csrf": csrf,
                },
                headers={"origin": miniapp.url, "hx-request": "true"},
            )
            assert "generation changed" in await stale.text()
            await expect(first.locator("#inspector")).to_have_attribute("data-pid", fresh_pid or "")
            await join(first)
            await join(second)
            await send(second, "after explicit rejoin")
            await expect(first.locator("#messages")).to_contain_text("after explicit rejoin")
            await send(other, "other room continues")
            await expect(other.locator("#messages")).to_contain_text("other room continues")
            await expect(other.locator("#inspector")).to_have_attribute("data-pid", other_pid or "")

            await second.goto(miniapp.url + "/rooms/beam")
            await expect(second.locator("h2")).to_have_text("# beam")
            await send(first, "old room event after navigation")
            await expect(second.locator("#messages")).not_to_contain_text("old room event")
            await asyncio.sleep(2.2)
            assert not errors
            await first.reload()
            await expect(first.locator("#messages")).to_contain_text("old room event")
            await other.close()
            await second.close()
            await first.goto("about:blank")
            async with asyncio.timeout(8):
                while True:
                    final = await resource_counts(first, miniapp.url)
                    if final.streams == final.subscriptions == final.operations == 0:
                        break
                    await asyncio.sleep(0.05)
            assert final.live_contexts == initial.live_contexts
            print(
                "Native room replacement:",
                old_pid,
                fresh_pid,
                "other room:",
                other_pid,
                "drained resources:",
                final.model_dump_json(),
            )
        finally:
            release_ack.set()
            await first_context.close()
            await second_context.close()
            await browser.close()
