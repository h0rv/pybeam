from contextlib import ExitStack

import httpx
import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from tests.conftest import ServerFactory
from tests.test_runtime_native import CounterReply

pytestmark = pytest.mark.runtime


def test_native__shutdown_runs_lifespan_and_closes_live_connection(
    runtime_server_factory: ServerFactory,
) -> None:
    with ExitStack() as clients:
        with runtime_server_factory("tests.fixtures.service_app:app") as server:
            peer = clients.enter_context(
                connect(
                    "ws://127.0.0.1:{}/shared".format(server.port),
                    open_timeout=5,
                    close_timeout=1,
                )
            )
            CounterReply.model_validate_json(peer.recv(timeout=3))
            response = httpx.get(server.base_url + "/subscriptions", timeout=3)
            response.raise_for_status()
            assert CounterReply.model_validate_json(response.content).count == 1
        assert server.process.returncode == 0
        assert "LIFESPAN_STOPPED" in server.log_path.read_text()
        with pytest.raises(ConnectionClosed):
            peer.recv(timeout=3)
