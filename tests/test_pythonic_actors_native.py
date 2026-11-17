import json
import os
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


@pytest.mark.runtime
@pytest.mark.parametrize(
    ("entry", "marker"),
    [
        ("examples/actors/main.py", "[1, 1, 1, 1, 1]"),
        ("tests/native/pythonic_actors.py", "pythonic-native-ok"),
        ("tests/fixtures/actor_inheritance.py", "pythonic-inheritance-ok"),
    ],
)
def test_pythonic_actors__run_through_managed_python_entrypoint(
    assets: RuntimeAssets, entry: str, marker: str
) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / entry)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker in result.stdout


@pytest.mark.runtime
def test_pythonic_actors__restart_requires_explicit_typed_rejoin(
    assets: RuntimeAssets, tmp_path: Path
) -> None:
    paths = json.dumps([str(ROOT / "src"), str(ROOT), *site.getsitepackages()])
    expression = """
        application:load(erlang_python),
        application:set_env(erlang_python, num_contexts, 1),
        application:set_env(erlang_python, event_loop_pool_size, 1),
        {ok, _} = application:ensure_all_started(erlang_python),
        ok = py_import:add_paths(PYTHON_PATHS),
        {ok, Ctx} = py_context:new(#{mode => worker}),
        ok = py_context:start_loop(Ctx),
        {ok, true} = py_context:submit_await(Ctx, 'tests.native.pythonic_actors',
            restart, [], #{}, 15000),
        ok = py_context:stop_loop(Ctx, 1000),
        receive {py_loop_exit, Ctx, _} -> ok after 5000 -> error(loop_survived) end,
        ok = py_context:stop(Ctx),
        ok = application:stop(erlang_python),
        io:format("pythonic-restart-ok~n"), halt(0).
    """.replace("PYTHON_PATHS", paths)
    environment = os.environ.copy()
    environment.update(assets.environment)
    environment["ERL_CRASH_DUMP"] = str(tmp_path / "erl_crash.dump")
    argv = [str(assets.executable), "+S", "2:2", "-noshell"]
    if assets.boot is not None:
        argv.extend(("-boot", str(assets.boot)))
    argv.extend(("-pa", *(str(path) for path in assets.code_paths), "-eval", expression))
    result = subprocess.run(
        argv,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pythonic-restart-ok" in result.stdout
