from pathlib import Path

import httpx
import pytest

from tests.conftest import ServerFactory

pytestmark = pytest.mark.runtime


def test_native__services_and_actors_ready_in_lifespan_before_requests(
    runtime_server_factory: ServerFactory,
) -> None:
    with runtime_server_factory("tests.fixtures.startup_app:app") as server:
        response = httpx.get(server.base_url + "/health")
        response.raise_for_status()
        assert response.json() == {"ok": True}


def test_native__lifespan_failure_exits_startup_with_original_error(
    runtime_server_factory: ServerFactory,
) -> None:
    with (
        pytest.raises(RuntimeError, match="Server exited during startup:") as failure,
        runtime_server_factory("tests.fixtures.startup_app:failed_app"),
    ):
        raise AssertionError("Failed startup served requests")
    log = Path(str(failure.value).partition(": ")[2]).read_text()
    assert "intentional lifespan startup failure" in log
    assert "HORNBEAM_START_ERROR" in log
