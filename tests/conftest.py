import os
import signal
import site
import socket
import subprocess
import time
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypeIs

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = ROOT / "_build" / "hornbeam"
RUNTIME_LAUNCHER = ROOT.parent / "hornbeam" / "examples" / "run_asgi.escript"
FRAMEWORK_TESTS = frozenset(
    {
        "test_browser_gate.py",
        "test_discord_demo.py",
        "test_discord_demo_browser.py",
        "test_fastapi.py",
        "test_modes_lab.py",
        "test_hornbeam_boundary.py",
        "test_litestar.py",
        "test_pyview_subscription.py",
        "test_distributed_showcase.py",
        "test_distributed_showcase_browser.py",
        "test_distributed_ready_browser.py",
        "test_distributed_active_browser.py",
        "test_distributed_active_harness.py",
        "test_showcase_shutdown.py",
        "test_showcase.py",
        "test_showcase_benchmarks.py",
        "test_showcase_browser.py",
        "test_showcase_provenance.py",
    }
)


@dataclass(frozen=True)
class RunningServer:
    base_url: str
    port: int
    log_path: Path
    process: subprocess.Popen[bytes]


type ServerFactory = Callable[[str], AbstractContextManager[RunningServer]]


def is_address(value: object) -> TypeIs[tuple[object, object]]:
    return isinstance(value, tuple) and len(value) == 2


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-frameworks", action="store_true", help="Collect optional framework tests"
    )
    parser.addoption("--run-benchmarks", action="store_true", help="Run guarded measurement tests")
    parser.addoption(
        "--run-runtime", action="store_true", help="Run tests on the real BEAM runtime"
    )
    parser.addoption(
        "--hornbeam-runtime", default=str(DEFAULT_RUNTIME), help="Built rebar3 profile directory"
    )
    parser.addoption(
        "--run-browser",
        action="store_true",
        help="Run Chromium example tests (requires --run-runtime)",
    )


def pytest_configure(config: pytest.Config) -> None:
    if not config.getoption("run_frameworks") and any(
        Path(argument.split("::", 1)[0]).name in FRAMEWORK_TESTS for argument in config.args
    ):
        raise pytest.UsageError("Optional framework test files require --run-frameworks")


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if not config.getoption("run_frameworks") and collection_path.name in FRAMEWORK_TESTS:
        return True
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("run_benchmarks"):
        measured = [item for item in items if item.get_closest_marker("benchmark") is not None]
        items[:] = [item for item in items if item.get_closest_marker("benchmark") is None]
        if measured:
            config.hook.pytest_deselected(items=measured)
    if config.getoption("run_browser"):
        if not config.getoption("run_runtime") and any(
            item.get_closest_marker("browser") is not None
            and item.get_closest_marker("runtime") is not None
            for item in items
        ):
            raise pytest.UsageError("--run-browser requires --run-runtime for native browser tests")
        return
    browser = [item for item in items if item.get_closest_marker("browser") is not None]
    if browser:
        items[:] = [item for item in items if item.get_closest_marker("browser") is None]
        config.hook.pytest_deselected(items=browser)


@pytest.fixture(scope="session")
def runtime_server_factory(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> ServerFactory:
    enabled: object = request.config.getoption("run_runtime")
    if enabled is not True:
        pytest.skip("Requires --run-runtime and a built Hornbeam fork")
    runtime_option: object = request.config.getoption("hornbeam_runtime")
    if not isinstance(runtime_option, str):
        raise TypeError("runtime path must be text")
    runtime = Path(runtime_option).resolve()
    logs = tmp_path_factory.mktemp("hornbeam")

    @contextmanager
    def start(app: str) -> Generator[RunningServer]:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            address: object = listener.getsockname()
            if not is_address(address):
                raise TypeError("Expected an IPv4 socket address")
            port = address[1]
            if not isinstance(port, int) or isinstance(port, bool):
                raise TypeError("Expected an integer TCP port")
        log_path = logs / "server-{}-{}.log".format(app.replace(":", "-"), port)
        argv = [
            "escript",
            str(RUNTIME_LAUNCHER),
            str(runtime),
            app,
            "127.0.0.1:" + str(port),
            "on",
            str(ROOT),
            str(ROOT / "src"),
            *site.getsitepackages(),
        ]
        base_url = "http://127.0.0.1:{}".format(port)
        with log_path.open("wb") as output:
            process = subprocess.Popen(
                argv,
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 30
                with httpx.Client(timeout=0.5) as client:
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError("Server exited during startup: {}".format(log_path))
                        try:
                            if client.get(base_url + "/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.05)
                    else:
                        raise TimeoutError("Server did not become ready: {}".format(log_path))
                yield RunningServer(base_url, port, log_path, process)
            finally:
                if process.poll() is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)

    return start


@pytest.fixture(scope="module")
def service_server(runtime_server_factory: ServerFactory) -> Generator[RunningServer]:
    with runtime_server_factory("tests.fixtures.service_app:app") as server:
        yield server
