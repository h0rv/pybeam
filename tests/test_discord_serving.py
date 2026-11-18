import socket
import subprocess
import sys
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


@dataclass(frozen=True, slots=True)
class NativeServer:
    url: str
    log: Path


@pytest.fixture
def native_http_server(request: pytest.FixtureRequest, tmp_path: Path) -> Generator[NativeServer]:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and staged native assets")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = "http://127.0.0.1:" + str(port)
    log = tmp_path / "native-serving.log"
    with log.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "tests.native.discord_serving", "--port", str(port)],
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            with httpx.Client(timeout=0.5) as client:
                while time.monotonic() < deadline:
                    assert process.poll() is None, log.read_text()
                    try:
                        response = client.get(url + "/health")
                    except httpx.TransportError:
                        time.sleep(0.05)
                        continue
                    assert response.status_code == 200, log.read_text()
                    assert response.text == "native-loop-ok"
                    break
                else:
                    pytest.fail("Native serving timed out: " + log.read_text())
            yield NativeServer(url, log)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                pytest.fail("Managed launcher did not exit: " + log.read_text())
    assert process.returncode == 143, log.read_text()
    assert "cleanup was not acknowledged" not in log.read_text()
    assert "PyBEAM cleanup failed" not in log.read_text()


@pytest.mark.runtime
def test_uvicorn__serves_inside_managed_native_loop(native_http_server: NativeServer) -> None:
    with httpx.Client() as client:
        response = client.get(native_http_server.url + "/health")
    assert response.status_code == 200
    assert response.text == "native-loop-ok"


@pytest.mark.runtime
def test_uvicorn__http_uses_lifespan_resources(native_http_server: NativeServer) -> None:
    with httpx.Client(base_url=native_http_server.url, timeout=5) as client:
        for _ in range(2):
            response = client.get("/resource")
            assert response.status_code == 200, native_http_server.log.read_text()
            assert response.text == "same-loop"


@pytest.mark.runtime
def test_uvicorn__websocket_uses_lifespan_resources(native_http_server: NativeServer) -> None:
    for _ in range(2):
        with connect(
            native_http_server.url.replace("http://", "ws://") + "/resource-ws", open_timeout=5
        ) as client:
            assert client.recv(timeout=5) == "same-loop", native_http_server.log.read_text()


@pytest.mark.runtime
@pytest.mark.browser
def test_uvicorn__two_chrome_contexts_use_managed_native_loop(
    request: pytest.FixtureRequest, native_http_server: NativeServer
) -> None:
    if request.config.getoption("run_browser") is not True:
        pytest.skip("Requires --run-browser and Google Chrome")
    if not CHROME.is_file():
        pytest.fail("--run-browser requested, but Google Chrome is not installed: " + str(CHROME))
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(CHROME), headless=True, chromium_sandbox=True
        )
        try:
            first = browser.new_context()
            second = browser.new_context()
            try:
                pages = (first.new_page(), second.new_page())
                for page in pages:
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("requestfailed", lambda request: errors.append(request.url))
                    page.goto(native_http_server.url + "/health")
                    assert page.locator("body").inner_text() == "native-loop-ok"
                pages[0].wait_for_timeout(2200)
                assert not errors
            finally:
                first.close()
                second.close()
        finally:
            browser.close()
