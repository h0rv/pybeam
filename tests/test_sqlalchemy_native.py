import asyncio
import json
import os
import signal
import site
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests.conftest import ROOT, RUNTIME_LAUNCHER, ServerFactory
from tests.native.managed_web import serve_entry

pytestmark = pytest.mark.runtime


class HornbeamAsyncStartupCrash(RuntimeError):
    pass


def test_sqlalchemy__actors_tasks_reuse_rollback_restart_and_dispose(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime")
    runtime: object = request.config.getoption("hornbeam_runtime")
    assert isinstance(runtime, str)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pybeam]\ndevelopment_runtime = " + json.dumps(str(Path(runtime).resolve())) + "\n"
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests/native/sqlalchemy_pool.py")],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT)))),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sqlalchemy-pools-ok" in result.stdout
    print(result.stdout.strip())


async def exercise_http(base_url: str, modes: tuple[str, ...] = ("sync", "async")) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=20) as client:
        for mode in modes:
            assert (await client.get("/notes/" + mode)).json() == []
            async with asyncio.TaskGroup() as group:
                calls = [
                    group.create_task(
                        client.post(route, json={"mode": mode, "text": route + str(i)})
                    )
                    for route in ("/notes", "/actors")
                    for i in range(4)
                ]
            assert all(call.result().status_code == 200 for call in calls)
            response = await client.post("/tasks/" + mode, params={"prefix": "fan-"})
            assert response.status_code == 200, response.text
            assert len(response.json()) == 4
            failed = await client.post(
                "/notes", json={"mode": mode, "text": "rollback", "fail": True}
            )
            assert failed.status_code == 409
            notes = (await client.get("/notes/" + mode)).json()
            assert len(notes) == 12
            assert "rollback" not in notes
        before = (await client.get("/pool-stats")).json()
        for _ in range(4):
            for mode in modes:
                assert (await client.get("/notes/" + mode)).status_code == 200
        after = (await client.get("/pool-stats")).json()
        for mode in modes:
            assert before[mode]["opened"] == after[mode]["opened"]
            assert 1 <= after[mode]["opened"] <= 2
            assert after[mode]["closed"] == 0
            assert after[mode]["checkouts"] >= 16


@pytest.mark.skipif(sys.platform != "linux", reason="Startup SIGSEGV was observed on Linux only")
@pytest.mark.xfail(
    strict=True,
    raises=HornbeamAsyncStartupCrash,
    reason="Pinned Hornbeam pooled-loop runner segfaults in greenlet during async engine startup; "
    "use the managed Uvicorn entrypoint (examples/sqlalchemy/README.md)",
)
def test_sqlalchemy__hornbeam_asgi_lifespan_pools(
    request: pytest.FixtureRequest,
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime")
    runtime: object = request.config.getoption("hornbeam_runtime")
    assert isinstance(runtime, str)
    result = subprocess.run(
        [
            "escript",
            str(RUNTIME_LAUNCHER),
            str(Path(runtime).resolve()),
            "tests.fixtures.sqlalchemy_app:app",
            "127.0.0.1:0",
            "on",
            str(ROOT),
            str(ROOT / "src"),
            *site.getsitepackages(),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode == -signal.SIGSEGV:
        raise HornbeamAsyncStartupCrash("Hornbeam async startup exited with SIGSEGV")
    pytest.fail(
        "Startup behavior changed; replace the constraint probe with an HTTP test: "
        + result.stdout
        + result.stderr
    )


def test_sqlalchemy__hornbeam_native_sync_engine(
    runtime_server_factory: ServerFactory,
) -> None:
    with runtime_server_factory("tests.fixtures.sqlalchemy_sync_app:app") as server:
        asyncio.run(exercise_http(server.base_url, ("sync",)))
    assert "http-pools-disposed" in server.log_path.read_text()


@pytest.mark.skipif(sys.platform == "linux", reason="Linux startup crash has a separate probe")
def test_sqlalchemy__hornbeam_native_async_engine(
    runtime_server_factory: ServerFactory,
) -> None:
    with runtime_server_factory("tests.fixtures.sqlalchemy_app:app") as server:
        asyncio.run(exercise_http(server.base_url))
    assert "http-pools-disposed" in server.log_path.read_text()


def test_sqlalchemy__managed_hornbeam_integration_http_and_shutdown(
    request: pytest.FixtureRequest, tmp_path: Path, unused_tcp_port: int
) -> None:
    with serve_entry(
        request,
        tmp_path,
        ROOT / "tests/native/sqlalchemy_http.py",
        unused_tcp_port,
        "--port",
        str(unused_tcp_port),
    ) as server:
        asyncio.run(exercise_http(server.base_url))
    assert "http-pools-disposed" in server.log_path.read_text()
