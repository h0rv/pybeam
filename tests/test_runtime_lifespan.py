from collections.abc import Generator

import httpx
import pytest
from websockets.sync.client import connect

from tests.conftest import RunningServer, ServerFactory

pytestmark = pytest.mark.runtime


@pytest.fixture(scope="module")
def lifespan_server(runtime_server_factory: ServerFactory) -> Generator[RunningServer]:
    with runtime_server_factory("tests.fixtures.lifespan_affinity_app:app") as server:
        yield server


def test_hornbeam__http_uses_lifespan_resources(lifespan_server: RunningServer) -> None:
    with httpx.Client(base_url=lifespan_server.base_url, timeout=5) as client:
        for _ in range(2):
            response = client.get("/resource")
            assert response.status_code == 200, lifespan_server.log_path.read_text()
            assert response.text == "same-loop"


def test_hornbeam__websocket_uses_lifespan_resources(lifespan_server: RunningServer) -> None:
    for _ in range(2):
        with connect(
            "ws://127.0.0.1:{}/resource-ws".format(lifespan_server.port), open_timeout=5
        ) as client:
            assert client.recv(timeout=5) == "same-loop", lifespan_server.log_path.read_text()
