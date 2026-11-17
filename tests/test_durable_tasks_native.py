import json
import os
import signal
import site
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pybeam.runtime.assets import discover_assets
from pybeam.runtime.config import Config
from tests.native.runtime import ROOT, require_runtime


@pytest.mark.runtime
def test_tasks__managed_submit_serve_and_crash(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    require_runtime(request)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(ROOT / "_build/hornbeam") + '"\n'
    )
    entry = ROOT / "tests/fixtures/durable_tasks.py"
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT))))
    result = subprocess.run(
        [sys.executable, str(entry), "proof"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TASKS_COMPLETED single_node" in result.stdout
    log = tmp_path / "writer.log"
    with log.open("w") as output:
        writer = subprocess.Popen(
            [sys.executable, str(entry), "put"],
            cwd=tmp_path,
            env=environment,
            stdout=output,
            stderr=output,
        )
        try:
            marker = tmp_path / "stored.pid"
            deadline = time.monotonic() + 30
            while not marker.exists():
                assert writer.poll() is None, log.read_text()
                assert time.monotonic() < deadline, log.read_text()
                time.sleep(0.01)
            vm_pid = int(marker.read_text())
            assert vm_pid != writer.pid
            os.kill(vm_pid, signal.SIGKILL)
            writer.wait(timeout=20)
        finally:
            if writer.poll() is None:
                writer.terminate()
                writer.wait(timeout=20)
    recovered = subprocess.run(
        [sys.executable, str(entry), "recover"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert "TASKS_RECOVERED acknowledged_submits" in recovered.stdout


@pytest.mark.runtime
def test_tasks__three_nodes_rerun_after_worker_node_dies(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    require_runtime(request)
    assets = discover_assets(Config())
    paths = json.dumps([str(ROOT / "src"), str(ROOT), *site.getsitepackages()])
    output = tmp_path / "completed"
    output.mkdir()
    for i in range(3):
        directory = tmp_path / str(i)
        directory.mkdir()
        (directory / "pyproject.toml").write_text('[tool.pybeam]\ndata_dir = "data"\n')
    boot_args = [] if assets.boot is None else ["-boot", str(assets.boot)]
    expression = """
        Parent = self(),
        Paths = lists:append([["-pa", P] || P <- code:get_path()]),
        Args = ["+S", "2:2", "+A", "2", "-setcookie", atom_to_list(erlang:get_cookie())]
               ++ BOOT_ARGS ++ Paths,
        Peers = [begin
            {ok, Peer, Node} = peer:start(#{name => Name, connection => standard_io,
                exec => EXECUTABLE, args => Args}),
            {Peer, Node}
        end || Name <- [durable_py_a, durable_py_b, durable_py_c]],
        try
            [pong = peer:call(P, net_adm, ping, [N])
             || {P, Local} <- Peers, {_, N} <- Peers, N =/= Local],
            Nodes = [atom_to_binary(N) || {_, N} <- Peers],
            Contexts = [begin
                ok = peer:call(P, file, set_cwd, [filename:join(DIRECTORY, integer_to_list(I))]),
                ok = peer:call(P, application, set_env, [erlang_python, num_contexts, 1]),
                ok = peer:call(P, application, set_env, [erlang_python, event_loop_pool_size, 1]),
                {ok, _} = peer:call(P, application, ensure_all_started, [erlang_python]),
                ok = peer:call(P, py_import, add_paths, [PYTHON_PATHS]),
                ok = peer:call(P, py_callback, register, [durable_node_ready,
                    fun([]) -> Parent ! {ready, node()}, ok end]),
                ok = peer:call(P, py_callback, register, [durable_body_started,
                    fun([Payload]) -> Parent ! {started, node(), Payload}, ok end]),
                {ok, Ctx} = peer:call(P, py_context, new, [#{mode => worker}]),
                ok = peer:call(P, py_context, start_loop, [Ctx]),
                {P, Ctx}
            end || {{P, _}, I} <- lists:zip(Peers, lists:seq(0, 2))],
            Opens = [begin
                Ref = make_ref(),
                spawn(fun() -> Parent ! {Ref, peer:call(P, py_context, submit_await,
                    [Ctx, 'tests.fixtures.durable_tasks', cluster_submit,
                         [Nodes, list_to_binary(OUTPUT), I, 1], #{}, 60000], 65000)} end),
                Ref
            end || {{P, Ctx}, I} <- lists:zip(Contexts, lists:seq(0, 2))],
            [receive {Ref, Reply} -> {ok, true} = Reply
             after 65000 -> error(open_timeout) end || Ref <- Opens],
            [{First, _}, _, {Dead, DeadNode}] = Peers,
            {ok, SubmitCtx} = peer:call(First, py_context, new, [#{mode => worker}]),
            {ok, true} = peer:call(First, py_context, submit_await,
                [SubmitCtx, 'tests.fixtures.durable_tasks', cluster_submit,
                 [Nodes, list_to_binary(OUTPUT), 3, 27], #{}, 15000], 20000),
            [{ok, _} = peer:call(P, py_context, submit, [Ctx, 'tests.fixtures.durable_tasks',
                cluster_serve, [Nodes]]) || {P, Ctx} <- Contexts],
            [receive {ready, N} -> ok after 40000 -> error({not_ready, N}) end || {_, N} <- Peers],
            Held = [receive {started, DeadNode, Payload} -> Payload
                    after 5000 -> error(no_dead_node_leases) end || _ <- [1, 2]],
            [false = filelib:is_file(binary_to_list(Payload)) || Payload <- Held],
            DeadPid = peer:call(Dead, os, getpid, []),
            os:cmd("kill -9 " ++ DeadPid),
            Wait = fun Again(0) -> error(tasks_not_completed);
                       Again(Left) ->
                           case lists:all(fun(I) -> filelib:is_file(
                               filename:join(OUTPUT, integer_to_list(I))) end, lists:seq(0, 29)) of
                               true -> ok;
                               false -> timer:sleep(50), Again(Left-1)
                           end
                   end,
            ok = Wait(500),
            [{ok, true} = peer:call(P, py_context, submit_await,
                [Ctx, 'tests.fixtures.durable_tasks', cluster_stop, [], #{}, 15000], 20000)
             || {P, Ctx} <- Contexts, P =/= Dead],
            io:format("TASKS_CLUSTER_COMPLETED 30 dead_leases=~p~n", [length(Held)])
        after
            [try peer:stop(P) catch _:_ -> ok end || {P, _} <- Peers]
        end,
        halt(0).
    """
    replacements = {
        "BOOT_ARGS": json.dumps(boot_args),
        "EXECUTABLE": json.dumps(str(assets.executable)),
        "DIRECTORY": json.dumps(str(tmp_path)),
        "OUTPUT": json.dumps(str(output)),
        "PYTHON_PATHS": paths,
    }
    for key, value in replacements.items():
        expression = expression.replace(key, value)
    environment = dict(os.environ, **assets.environment)
    environment["ERL_CRASH_DUMP"] = str(tmp_path / "erl_crash.dump")
    argv = [str(assets.executable), "+S", "2:2", "-noshell", "-sname", "durable_py_controller"]
    argv.extend(boot_args)
    argv.extend(("-pa", *(str(path) for path in assets.code_paths), "-eval", expression))
    result = subprocess.run(
        argv,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=100,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TASKS_CLUSTER_COMPLETED 30 dead_leases=2" in result.stdout
