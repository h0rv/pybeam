import asyncio
import hashlib
import socket
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil
import pytest
from playwright.async_api import Page, Request, Route, async_playwright, expect

from examples.discord_demo.main import Resources
from pybeam.runtime.ownership import identity, resolve

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
    assets = ROOT / "src/pybeam/runtime/_assets"
    fingerprints = {
        str(path.relative_to(assets)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in assets.rglob("*")
        if path.is_file()
    }
    assert len(fingerprints) == 723
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
            owned = tuple(
                identity(child)
                for child in (
                    psutil.Process(process.pid),
                    *psutil.Process(process.pid).children(recursive=True),
                )
            )
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
    assert all(resolve(birth) is None for birth in owned)
    assert fingerprints == {
        str(path.relative_to(assets)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in assets.rglob("*")
        if path.is_file()
    }
    text = log.read_text()
    assert process.returncode == 143, text
    assert "Application shutdown complete" in text, text
    assert "Application shutdown failed" not in text, text
    assert "Traceback" not in text or "Slow SSE peer exceeded" in text, text
    assert "PyBeam cleanup failed" not in text, text
    assert "cleanup was not acknowledged" not in text, text


async def test_miniapp__clear_first_screen_origin_forms_and_shared_restart(
    miniapp: Miniapp, tmp_path: Path
) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=str(CHROME), headless=True, chromium_sandbox=True
        )
        try:
            desktop = await browser.new_context(viewport={"width": 1440, "height": 900})
            phone = await browser.new_context(viewport={"width": 390, "height": 844})
            first, second = await desktop.new_page(), await phone.new_page()
            errors: list[str] = []
            for page, label in ((first, "desktop"), (second, "phone")):
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(miniapp.url)
                await expect(page.get_by_role("button", name="Join #general")).to_be_in_viewport()
                await expect(page.get_by_label("Message", exact=True)).to_be_in_viewport()
                await expect(page.locator("#inspector")).not_to_be_visible()
                await page.screenshot(path=str(tmp_path / ("chat-ready-" + label + ".png")))
                print(
                    "Chat viewport",
                    label,
                    "join",
                    await page.locator("#join-room").bounding_box(),
                    "composer",
                    await page.locator("#message").bounding_box(),
                )
                await join(page)
                await expect(page.get_by_role("button", name="Send message")).to_be_enabled()
            assert (await desktop.cookies())[0]["value"] != (await phone.cookies())[0]["value"]
            await send(second, "Hello from a private session")
            for page in (first, second):
                await expect(page.locator("#messages")).to_contain_text(
                    "Hello from a private session"
                )
                await expect(page.locator("#messages")).to_be_in_viewport()
                assert await page.evaluate("window.scrollY") == 0
            captured = {
                "generation": await first.locator(
                    'input[name="generation"][form="compose"]'
                ).input_value(),
                "csrf": await first.locator('input[name="csrf"][form="compose"]').input_value(),
            }
            before = await first.locator("#messages").inner_text()
            for origin in (
                "null",
                "http://localhost:" + miniapp.url.rsplit(":", 1)[1],
                "https://attacker.invalid",
            ):
                response = await first.request.post(
                    miniapp.url + "/rooms/general/actions",
                    form={**captured, "action": "send", "text": "must not appear"},
                    headers={"origin": origin},
                )
                assert response.status == 403
            await first.reload()
            await expect(first.locator("#messages")).to_contain_text("Hello from a private session")
            assert await first.locator("#messages").inner_text() == before
            admitted = asyncio.Event()
            release = asyncio.Event()

            async def hold(route: Route) -> None:
                response = await route.fetch()
                admitted.set()
                await release.wait()
                await route.fulfill(response=response)

            await first.route("**/actions", hold)
            await send(first, "Held reply")
            async with asyncio.timeout(5):
                await admitted.wait()
            await send(second, "Newer message")
            await expect(first.locator("#messages")).to_contain_text("Newer message")
            release.set()
            await expect(first.locator("#ack")).to_contain_text("Confirmed send")
            await expect(first.locator("#messages")).to_contain_text("Newer message")
            await first.unroute("**/actions", hold)
            old_pid = await first.locator("#inspector").get_attribute("data-pid")
            await first.get_by_role("button", name="Restart room (clears messages)").click()
            for page, label in ((first, "desktop"), (second, "phone")):
                await expect(page.locator("#inspector")).not_to_have_attribute(
                    "data-pid", old_pid or ""
                )
                await expect(page.locator(".restart-notice")).to_be_in_viewport()
                await expect(page.locator("#messages")).not_to_contain_text("Newer message")
                await expect(page.get_by_role("button", name="Send message")).to_be_disabled()
                await page.screenshot(path=str(tmp_path / ("chat-restarted-" + label + ".png")))
            fresh_pid = await first.locator("#inspector").get_attribute("data-pid")
            stale = await first.request.post(
                miniapp.url + "/rooms/general/actions",
                form={**captured, "action": "crash"},
                headers={"origin": miniapp.url, "hx-request": "true"},
            )
            assert "generation changed" in await stale.text()
            await expect(first.locator("#inspector")).to_have_attribute("data-pid", fresh_pid or "")
            await join(first)
            await join(second)
            await send(second, "After explicit rejoin")
            await expect(first.locator("#messages")).to_contain_text("After explicit rejoin")
            for page, label in ((first, "desktop"), (second, "phone")):
                await page.screenshot(path=str(tmp_path / ("chat-after-" + label + ".png")))
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
            plain = await browser.new_context(java_script_enabled=False)
            page = await plain.new_page()
            await page.goto(miniapp.url)
            await join(page)
            await page.get_by_label("Message", exact=True).fill("Normal form works")
            await page.get_by_role("button", name="Send message").click()
            await expect(page.locator("#messages")).to_contain_text("Normal form works")
            await expect(first.locator("#messages")).to_contain_text("Normal form works")
            assert not errors
            await plain.close()
            await phone.close()
            await desktop.close()
        finally:
            await browser.close()


async def test_miniapp__shutdown_drains_live_native_stream(miniapp: Miniapp) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=str(CHROME), headless=True, chromium_sandbox=True
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(miniapp.url + "/rooms/general")
            await expect(page.locator("#inspector")).to_be_attached()
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
        browser = await playwright.chromium.launch(
            executable_path=str(CHROME), headless=True, chromium_sandbox=True
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(miniapp.url + "/rooms/general")
            await expect(page.locator("#inspector")).to_be_attached()
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


async def test_miniapp__leave_rejoin_rejects_captured_old_form_and_preserves_receipts(
    miniapp: Miniapp,
) -> None:
    errors: list[str] = []
    navigating = False

    def request_failed(request: Request) -> None:
        if navigating and request.url.endswith("/events") and request.failure == "net::ERR_ABORTED":
            return
        errors.append(request.url + ": " + str(request.failure))

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=str(CHROME), headless=True, chromium_sandbox=True
        )
        first_context = await browser.new_context()
        second_context = await browser.new_context()
        release_ack = asyncio.Event()
        try:
            first, second = await first_context.new_page(), await second_context.new_page()
            for page in (first, second):
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("requestfailed", request_failed)
                await page.goto(miniapp.url + "/rooms/general")
                await expect(page.locator("#inspector")).to_be_attached()
                await join(page)
            baseline = await resource_counts(first, miniapp.url)
            assert baseline.streams == baseline.subscriptions == 2
            await send(first, "one: both native recipients")
            for page in (first, second):
                await expect(page.locator("#members li")).to_have_count(2)
                await expect(page.locator('#members li[data-count="1"]')).to_have_count(2)
            form = second.locator("form").filter(
                has=second.get_by_role("button", name="Leave", exact=True)
            )
            captured: dict[str, str] = {}
            for element in await form.locator("input[name]").all():
                name = await element.get_attribute("name")
                assert name is not None
                captured[name] = await element.input_value()
            assert captured["revision"] == "0"
            second_cookie = next(
                item["value"]
                for item in await second_context.cookies()
                if item["name"] == "discord"
            )
            second_id = second_cookie.split(".", 1)[0]
            second_ledger = '#ledger li[data-member="' + second_id + '"]'
            completed = asyncio.Event()

            async def delay_leave_ack(route: Route) -> None:
                response = await route.fetch()
                completed.set()
                await release_ack.wait()
                await route.fulfill(response=response)

            await second.route("**/actions", delay_leave_ack)
            await second.get_by_role("button", name="Leave", exact=True).click()
            async with asyncio.timeout(5):
                await completed.wait()
            for page in (first, second):
                await expect(page.locator("#members li")).to_have_count(1)
                await expect(page.locator(second_ledger)).to_have_attribute("data-present", "false")
            await send(first, "two: only the remaining native recipient")
            for page in (first, second):
                await expect(page.locator("#messages")).to_contain_text("two: only")
                await expect(page.locator('#members li[data-count="2"]')).to_have_count(1)
                await expect(page.locator(second_ledger)).to_have_attribute("data-count", "1")
            release_ack.set()
            await expect(second.locator("#ack")).to_contain_text("Confirmed leave")
            await expect(second.locator("#messages")).to_contain_text("two: only")
            await expect(second.locator("#members li")).to_have_count(1)
            await second.unroute("**/actions", delay_leave_ack)
            await join(second)
            for page in (first, second):
                await expect(page.locator("#members li")).to_have_count(2)
                await expect(page.locator(second_ledger)).to_have_attribute("data-revision", "1")
                await expect(page.locator(second_ledger)).to_have_attribute("data-present", "true")
                await expect(page.locator("#inspector")).to_have_attribute(
                    "data-generation", captured["generation"]
                )
            response = await second.request.post(
                miniapp.url + "/rooms/general/actions",
                form={**captured},
                headers={"origin": miniapp.url, "hx-request": "true"},
            )
            rejection = await response.text()
            assert "Not confirmed" in rejection and "revision" in rejection
            impersonation = await first.request.post(
                miniapp.url + "/rooms/general/actions",
                form={**captured, "revision": "1"},
                headers={"origin": miniapp.url, "hx-request": "true"},
            )
            assert impersonation.status == 403
            forged_member = await second.request.post(
                miniapp.url + "/rooms/general/actions",
                form={**captured, "revision": "1", "member_id": "another-session"},
                headers={"origin": miniapp.url, "hx-request": "true"},
            )
            assert forged_member.status == 422
            assert not errors
            navigating = True
            await second.reload()
            await expect(second.locator(second_ledger)).to_have_attribute("data-revision", "1")
            navigating = False
            await expect(second.locator(second_ledger)).to_have_attribute("data-count", "1")
            await expect(second.locator("#members li")).to_have_count(2)
            await send(first, "three: both native recipients after rejoin")
            for page in (first, second):
                await expect(page.locator("#messages")).to_contain_text("three: both")
                await expect(page.locator('#members li[data-count="3"]')).to_have_count(1)
                await expect(page.locator(second_ledger)).to_have_attribute("data-count", "2")
                await expect(page.locator("#identity-slots")).to_contain_text("2 / 8")
            await asyncio.sleep(2.2)
            assert not errors
            await second.close()
            await first.goto("about:blank")
            async with asyncio.timeout(8):
                while True:
                    final = await resource_counts(first, miniapp.url)
                    if final.streams == final.subscriptions == final.operations == 0:
                        break
                    await asyncio.sleep(0.05)
            assert final.live_contexts == baseline.live_contexts
            print(
                "Native leave/rejoin verified: room generation",
                captured["generation"],
                "membership revision 0 -> 1; native receipts A=3 B=2; stale form rejected;",
                "drained resources:",
                final.model_dump_json(),
            )
        finally:
            release_ack.set()
            await first_context.close()
            await second_context.close()
            await browser.close()


async def join(page: Page) -> None:
    await page.get_by_role("button", name="Join #").click()
    await expect(page.locator("#membership")).to_have_text("Joined")


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
        browser = await playwright.chromium.launch(
            executable_path=str(CHROME), headless=True, chromium_sandbox=True
        )
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
            await expect(first.locator("#inspector")).to_be_attached()
            await second.goto(miniapp.url + "/rooms/general")
            await expect(second.locator("#inspector")).to_be_attached()
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
            await first.get_by_role("button", name="Restart room (clears messages)").click()
            await expect(first.locator("#inspector")).not_to_have_attribute(
                "data-pid", old_pid or ""
            )
            for page in (first, second):
                await expect(page.locator("#members li")).to_have_text(["No one has joined."])
                await expect(page.locator("#messages")).to_contain_text("No messages")
                await expect(page.locator("#membership")).to_contain_text("Join to send.")
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
