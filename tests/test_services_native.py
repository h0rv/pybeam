import os
import subprocess
from pathlib import Path

import pytest

from tests.native.runtime import ROOT, host_python_paths, require_runtime


@pytest.mark.runtime
def test_services__native_worker_without_web_server(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    runtime = require_runtime(request)
    paths = [ROOT / "src", ROOT / "tests" / "fixtures"]
    path_code = repr([str(path) for path in paths])
    code = """
        application:load(erlang_python),
        application:set_env(erlang_python, num_contexts, 1),
        application:set_env(erlang_python, event_loop_pool_size, 1),
        {ok, _} = application:ensure_all_started(erlang_python),
        ok = py_import:add_paths(HOST_PATHS),
        {ok, W} = py_context:new(#{mode => worker}),
        ok = py_context:exec(W, <<"import sys; sys.path[:0] = PATHS">>),
        ok = py_context:exec(W, <<"import services_worker">>),
        ok = py_context:start_loop(W),
        {ok, Task} = py_context:submit(W, services_worker, verify, []),
        Owner = self(),
        {Other, Mon} = spawn_monitor(fun() ->
            Owner ! {other_waiter, py_event_loop:await(Task, 10)}
        end),
        receive {other_waiter, {error, timeout}} -> ok after 5000 -> error(wrong_owner) end,
        receive {'DOWN', Mon, process, Other, normal} -> ok after 5000 -> error(waiter_leaked) end,
        {ok, <<"services-ok">>} = py_event_loop:await(Task, 10000),
        {ok, Channel} = py_channel:new(#{max_size => 1024}),
        busy = py_channel:send(Channel, binary:copy(<<0>>, 2048)),
        {ok, _} = py_context:submit_await(W, services_worker, verify_channel, [Channel], #{}, 5000),
        #{closed := true, size := 0, max_size := 1024} = py_channel:info(Channel),
        {error, closed} = py_channel:send(Channel, <<"late">>),
        ok = py_context:stop_loop(W, 1000),
        receive {py_loop_exit, W, _} -> ok after 5000 -> error(missing_loop_exit) end,
        ContextMon = erlang:monitor(process, W),
        ok = py_context:stop(W),
        receive {'DOWN', ContextMon, process, W, _} -> ok after 5000 -> error(context_leaked) end,
        false = is_process_alive(W),
        false = lists:keymember(hornbeam, 1, application:which_applications()),
        ok = application:stop(erlang_python),
        io:format("services-native-ok~n"),
        halt(0).
    """.replace("HOST_PATHS", host_python_paths()).replace("PATHS", path_code)
    environment = os.environ.copy()
    environment["ERL_CRASH_DUMP"] = str(tmp_path / "erl_crash.dump")
    result = subprocess.run(
        ["erl", "+S", "2:2", "-noshell", "-pa", str(runtime / "ebin"), "-eval", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    (tmp_path / "services.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "services-native-ok" in result.stdout
