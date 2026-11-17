import json
import os
import selectors
import site
import subprocess
import sys
from pathlib import Path

import pytest

from pybeam.runtime.assets import RuntimeAssets, discover_assets
from pybeam.runtime.config import Config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def assets(request: pytest.FixtureRequest) -> RuntimeAssets:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and staged native actor assets")
    return discover_assets(Config())


def command(assets: RuntimeAssets, directory: Path, method: str, *, wait: bool = False) -> list[str]:
    paths = json.dumps([str(ROOT / "src"), str(ROOT), *site.getsitepackages()])
    expression = """
        application:load(erlang_python),
        application:set_env(erlang_python, num_contexts, 1),
        application:set_env(erlang_python, event_loop_pool_size, 1),
        {ok, _} = application:ensure_all_started(erlang_python),
        ok = py_import:add_paths(PATHS),
        {ok, Ctx} = py_context:new(#{mode => worker}),
        ok = py_context:start_loop(Ctx),
        {ok, true} = py_context:submit_await(Ctx, 'tests.native.actor_state',
            METHOD, [], #{}, 20000),
        io:format("actor-state-ok~n"),
        FINISH
    """.replace("PATHS", paths).replace("METHOD", method).replace(
        "FINISH", "receive never -> ok end." if wait else "halt(0)."
    )
    # The managed configuration resolves data_dir relative to the working directory.
    (directory / "pyproject.toml").write_text('[tool.pybeam]\ndata_dir = "data"\n')
    argv = [str(assets.executable), "+S", "2:2", "-noshell"]
    if assets.boot is not None:
        argv.extend(("-boot", str(assets.boot)))
    return [*argv, "-pa", *(str(path) for path in assets.code_paths), "-eval", expression]


@pytest.mark.runtime
def test_state_survives_actor_restart(assets: RuntimeAssets, tmp_path: Path) -> None:
    environment = dict(os.environ, **assets.environment)
    result = subprocess.run(
        command(assets, tmp_path, "actor_restart"), cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=40, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "actor-state-ok" in result.stdout


@pytest.mark.runtime
def test_acknowledged_state_survives_sigkill_of_whole_vm(
    assets: RuntimeAssets, tmp_path: Path
) -> None:
    environment = dict(os.environ, **assets.environment)
    with subprocess.Popen(
        command(assets, tmp_path, "prepare_crash", wait=True), cwd=tmp_path, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    ) as process:
        assert process.stdout is not None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                output = b""
                while b"actor-state-ok" not in output:
                    assert selector.select(timeout=40), output.decode(errors="replace")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    assert chunk, output.decode(errors="replace")
                    output += chunk
        finally:
            process.kill()
            process.wait(timeout=10)
    result = subprocess.run(
        command(assets, tmp_path, "verify_crash"), cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=40, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "actor-state-ok" in result.stdout
