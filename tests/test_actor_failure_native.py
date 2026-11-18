import json
import os
import site
import subprocess
from pathlib import Path

import pytest

from pybeam.runtime.assets import discover_assets
from pybeam.runtime.config import Config
from tests.native.runtime import ROOT


@pytest.mark.runtime
@pytest.mark.parametrize("attempt", range(3))
def test_native__queued_call_observes_actor_crash(
    request: pytest.FixtureRequest, tmp_path: Path, attempt: int
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime")
    profile: object = request.config.getoption("hornbeam_runtime")
    assert isinstance(profile, str)
    assets = discover_assets(Config(development_runtime=Path(profile)))
    paths = json.dumps([str(ROOT / "src"), str(ROOT), *site.getsitepackages()])
    expression = """
        application:load(erlang_python),
        application:set_env(erlang_python, num_contexts, 1),
        application:set_env(erlang_python, event_loop_pool_size, 1),
        {ok, _} = application:ensure_all_started(erlang_python),
        ok = py_import:add_paths(PATHS),
        ok = py_callback:register(failure_probe_crash, fun([Pid]) ->
            exit(list_to_pid(binary_to_list(Pid)), kill), ok
        end),
        {ok, Ctx} = py_context:new(#{mode => worker}),
        ok = py_context:start_loop(Ctx),
        {ok, true} = py_context:submit_await(Ctx, 'tests.native.actor_failure',
            verify, [], #{}, 10000),
        halt(0).
    """.replace("PATHS", paths)
    result = subprocess.run(
        [
            str(assets.executable),
            "+S",
            "2:2",
            "-noshell",
            "-pa",
            *(str(path) for path in assets.code_paths),
            "-eval",
            expression,
        ],
        cwd=ROOT,
        env=dict(os.environ, **assets.environment, ERL_CRASH_DUMP=str(tmp_path / "crash.dump")),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "failure-probe RuntimeError RuntimeError" in result.stdout
