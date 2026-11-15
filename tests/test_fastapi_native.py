import time

import httpx
import pytest

from tests.conftest import ServerFactory

pytestmark = pytest.mark.runtime


def test_native__fastapi_example_uses_form_validation_and_push(
    runtime_server_factory: ServerFactory,
) -> None:
    with (
        runtime_server_factory("examples.fastapi.main:app") as server,
        httpx.Client(base_url=server.base_url, timeout=5) as client,
    ):
        assert client.get("/").status_code == 200
        assert client.post("/increment", data={"amount": "0"}).status_code == 422
        with client.stream("GET", "/events") as events:
            events.raise_for_status()
            lines = events.iter_lines()
            assert next(lines) == 'data: <output id="count">0</output>'
            assert next(lines) == ""
            time.sleep(2.1)
            response = client.post(
                "/increment", data={"amount": "2"}, headers={"hx-request": "true"}
            )
            assert response.status_code == 200
            assert response.text == "Added 2."
            assert next(lines) == 'data: <output id="count">2</output>'
        assert client.get("/counter").text == '<output id="count">2</output>'
