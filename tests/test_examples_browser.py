import os
import re
from collections.abc import Generator
from typing import Literal

import pytest
from playwright.sync_api import (
    APIResponse,
    Browser,
    ConsoleMessage,
    Error,
    Frame,
    Request,
    Response,
    Route,
    WebSocket,
    expect,
    sync_playwright,
)

from tests.conftest import ServerFactory

pytestmark = [pytest.mark.runtime, pytest.mark.browser]


@pytest.fixture(scope="session")
def chromium(request: pytest.FixtureRequest) -> Generator[Browser]:
    browser_enabled: object = request.config.getoption("run_browser")
    runtime_enabled: object = request.config.getoption("run_runtime")
    if browser_enabled is not True or runtime_enabled is not True:
        pytest.skip("Requires both --run-browser and --run-runtime")
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
                headless=True,
            )
        except Error as error:
            pytest.fail(
                "Chromium unavailable. Run `uv run playwright install chromium` or set "
                "PLAYWRIGHT_CHROMIUM_EXECUTABLE. " + str(error)
            )
        try:
            yield browser
        finally:
            browser.close()


@pytest.mark.parametrize("framework", ["fastapi", "litestar", "pyview"])
def test_counter__two_browsers_should_share_updates_without_navigation(
    framework: Literal["fastapi", "litestar", "pyview"],
    chromium: Browser,
    runtime_server_factory: ServerFactory,
) -> None:
    errors: list[str] = []
    navigations: list[str] = []
    sockets: list[WebSocket] = []
    closed_sockets: list[WebSocket] = []
    streams: list[Response] = []
    mutations: list[int] = []
    posts: list[str] = []

    def console(message: ConsoleMessage) -> None:
        if message.type == "error":
            errors.append(message.text)

    def page_error(error: Error) -> None:
        errors.append(str(error))

    def navigated(frame: Frame) -> None:
        if frame.parent_frame is None:
            navigations.append(frame.url)

    def socket_closed(connection: WebSocket) -> None:
        closed_sockets.append(connection)

    def socket_opened(connection: WebSocket) -> None:
        sockets.append(connection)
        connection.on("close", socket_closed)

    def requested(request: Request) -> None:
        if request.method == "POST":
            posts.append(request.url)

    def responded(response: Response) -> None:
        if response.url.endswith("/events"):
            streams.append(response)
        elif response.url.endswith("/increment"):
            mutations.append(response.status)

    selector = "#count" if framework == "fastapi" else "#counter"
    with (
        runtime_server_factory("examples." + framework + ".main:app") as server,
        chromium.new_context() as first_context,
        chromium.new_context() as second_context,
    ):
        first, second = first_context.new_page(), second_context.new_page()
        for page in (first, second):
            page.on("console", console)
            page.on("pageerror", page_error)
            page.on("framenavigated", navigated)
            page.on("websocket", socket_opened)
            page.on("request", requested)
            page.on("response", responded)
            if framework == "pyview":
                with page.expect_websocket():
                    page.goto(server.base_url)
                expect(page.locator("[data-phx-main]")).to_have_class(re.compile("phx-connected"))
            else:
                with page.expect_response("**/events") as connected:
                    page.goto(server.base_url)
                assert connected.value.status == 200
                assert "text/event-stream" in connected.value.headers["content-type"]
            expect(page.locator(selector)).to_have_text("0")
            page.locator('input[name="amount"]').fill("10")
        initial_navigations = tuple(navigations)
        assert len(sockets) == (2 if framework == "pyview" else 0)
        assert len(streams) == (0 if framework == "pyview" else 2)
        first.get_by_role("button", name="Increment", exact=True).click()
        for page in (first, second):
            expect(page.locator(selector)).to_have_text("10", timeout=10000)
            expect(page.locator('input[name="amount"]')).to_have_value("10")
        first.wait_for_timeout(2200)
        second.get_by_role("button", name="Increment", exact=True).click()
        for page in (first, second):
            expect(page.locator(selector)).to_have_text("20", timeout=10000)
            expect(page.locator('input[name="amount"]')).to_have_value("10")
        if framework != "pyview":
            held: list[tuple[Route, APIResponse]] = []

            def delay_response(route: Route) -> None:
                response = route.fetch()
                assert response.status == 200
                held.append((route, response))

            first.route("**/increment", delay_response)
            first.get_by_role("button", name="Increment", exact=True).click()
            for page in (first, second):
                expect(page.locator(selector)).to_have_text("30", timeout=10000)
            expect(first.locator("form")).to_have_class(re.compile("htmx-request"))
            assert len(held) == 1
            second.get_by_role("button", name="Increment", exact=True).click()
            for page in (first, second):
                expect(page.locator(selector)).to_have_text("40", timeout=10000)
            route, response = held[0]
            route.fulfill(response=response)
            expect(first.locator("form")).not_to_have_class(re.compile("htmx-request"))
            for page in (first, second):
                expect(page.locator(selector)).to_have_text("40", timeout=10000)
                expect(page.locator('input[name="amount"]')).to_have_value("10")
            current = first_context.request.get(server.base_url + "/counter")
            assert current.status == 200
            expected_counter = (
                '<output id="count">40</output>'
                if framework == "fastapi"
                else '<span id="counter">40</span>'
            )
            assert current.text() == expected_counter
        assert tuple(navigations) == initial_navigations
        assert len(sockets) == (2 if framework == "pyview" else 0)
        assert not closed_sockets
        assert len(streams) == (0 if framework == "pyview" else 2)
        assert len(posts) == (0 if framework == "pyview" else 4)
        assert mutations == ([] if framework == "pyview" else [200, 200, 200, 200])
        assert not errors, errors
