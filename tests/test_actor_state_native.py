import json
import os
import selectors
import site
import subprocess
from pathlib import Path

import pytest

from pybeam.runtime.assets import RuntimeAssets, discover_assets
from pybeam.runtime.config import Config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def assets(request: pytest.FixtureRequest) -> RuntimeAssets:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and staged native actor assets")
    runtime: object = request.config.getoption("hornbeam_runtime")
    if not isinstance(runtime, str):
        raise TypeError("runtime path must be text")
    return discover_assets(Config(development_runtime=Path(runtime)))


def command(
    assets: RuntimeAssets, directory: Path, method: str, *, wait: bool = False
) -> list[str]:
    paths = json.dumps([str(ROOT / "src"), str(ROOT), *site.getsitepackages()])
    expression = (
        """
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
    """.replace("PATHS", paths)
        .replace("METHOD", method)
        .replace("FINISH", "receive never -> ok end." if wait else "halt(0).")
    )
    # The managed configuration resolves data_dir relative to the working directory.
    (directory / "pyproject.toml").write_text('[tool.pybeam]\ndata_dir = "data"\n')
    argv = [str(assets.executable), "+S", "2:2", "-noshell"]
    if assets.boot is not None:
        argv.extend(("-boot", str(assets.boot)))
    return [*argv, "-pa", *(str(path) for path in assets.code_paths), "-eval", expression]


@pytest.mark.runtime
@pytest.mark.parametrize(
    "method", ["actor_restart", "ownership_lifecycle", "cancel_standby", "replica_validation"]
)
def test_state_survives_actor_restart(assets: RuntimeAssets, tmp_path: Path, method: str) -> None:
    environment = dict(os.environ, **assets.environment)
    result = subprocess.run(
        command(assets, tmp_path, method),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "actor-state-ok" in result.stdout


@pytest.mark.runtime
def test_three_nodes_take_over_saved_state_after_owner_sigkill(
    assets: RuntimeAssets, tmp_path: Path
) -> None:
    for i in range(3):
        directory = tmp_path / str(i)
        directory.mkdir()
        (directory / "pyproject.toml").write_text('[tool.pybeam]\ndata_dir = "data"\n')
    expression = """
        Parent = self(),
        Paths = lists:append([["-pa", P] || P <- code:get_path()]),
        Args = ["+S", "2:2", "+A", "2", "-setcookie", atom_to_list(erlang:get_cookie())]
               ++ BOOT_ARGS ++ Paths,
        Peers = [begin
            {ok, Peer, Node} = peer:start(#{name => Name, host => "127.0.0.1",
                longnames => true, connection => standard_io,
                exec => EXECUTABLE, args => Args}),
            {Peer, Node}
        end || Name <- [actor_state_a, actor_state_b, actor_state_c]],
        try
            [pong = peer:call(P, net_adm, ping, [N])
             || {P, Local} <- Peers, {_, N} <- Peers, N =/= Local],
            Nodes = [atom_to_binary(N) || {_, N} <- Peers],
            Contexts = [begin
                Dir = filename:join(DIRECTORY, integer_to_list(I)),
                ok = peer:call(P, file, set_cwd, [Dir]),
                true = peer:call(P, os, putenv, ["ERL_CRASH_DUMP", filename:join(Dir, "erl_crash.dump")]),
                ok = peer:call(P, application, set_env, [erlang_python, num_contexts, 1]),
                ok = peer:call(P, application, set_env, [erlang_python, event_loop_pool_size, 1]),
                {ok, _} = peer:call(P, application, ensure_all_started, [erlang_python]),
                ok = peer:call(P, py_import, add_paths, [PYTHON_PATHS]),
                ok = peer:call(P, py_callback, register, [actor_owned,
                    fun([Count]) -> Parent ! {owned, node(), Count}, ok end]),
                ok = peer:call(P, py_callback, register, [actor_saved,
                    fun([Count]) -> Parent ! {saved, node(), Count}, ok end]),
                ok = peer:call(P, sys, install, [py_actor_state_owner,
                    {fun(State, Event, _) ->
                        case Event of
                            {in, {'$gen_call', _, {claim, _, _}}} -> State ! {claim, node()};
                            _ -> ok
                        end,
                        State
                     end, Parent}]),
                {ok, Ctx} = peer:call(P, py_context, new, [#{mode => worker}]),
                ok = peer:call(P, py_context, start_loop, [Ctx, #{owner => Parent}]),
                {P, N, Ctx}
            end || {{P, N}, I} <- lists:zip(Peers, lists:seq(0, 2))],
            [{ok, _} = peer:call(P, py_context, submit,
                [Ctx, 'tests.native.actor_state', cluster_run, [Nodes]]) || {P, _, Ctx} <- Contexts],
            [receive {claim, N} -> ok after 40000 -> error({no_claim, N}) end || {_, N} <- Peers],
            DeadNode = receive {owned, Owner, 0} -> Owner after 10000 -> error(no_owner) end,
            receive {saved, DeadNode, 5} -> ok after 10000 -> error(no_saved_ack) end,
            Status = fun(P) ->
                #{claims := Claims} = peer:call(P, sys, get_state, [py_actor_state_owner]),
                [Claim] = maps:values(Claims),
                maps:get(status, Claim)
            end,
            [owned, waiting, waiting] = lists:sort([Status(P) || {P, _} <- Peers]),
            [{Dead, DeadNode}] = [{P, N} || {P, N} <- Peers, N =:= DeadNode],
            DeadPid = peer:call(Dead, os, getpid, []),
            os:cmd("kill -9 " ++ DeadPid),
            Winner = receive {owned, Survivor, 5} -> Survivor
                     after 30000 -> error(no_takeover) end,
            true = Winner =/= DeadNode,
            [owned, waiting] = lists:sort([Status(P) || {P, N} <- Peers, N =/= DeadNode]),
            receive {owned, Other, Count} -> error({duplicate_owner, Other, Count})
            after 500 -> ok end,
            [{ok, true} = peer:call(P, py_context, submit_await,
                [Ctx, 'tests.native.actor_state', cluster_stop, [], #{}, 15000], 20000)
             || {P, N, Ctx} <- Contexts, N =/= DeadNode],
            [begin
                #{claims := Empty} = peer:call(P, sys, get_state, [py_actor_state_owner]),
                0 = map_size(Empty)
             end || {P, N} <- Peers, N =/= DeadNode],
            io:format("ACTOR_CLUSTER_OK count=5 owners=1 standby=1 clean_stop~n")
        after
            [try peer:stop(P) catch _:_ -> ok end || {P, _} <- Peers]
        end,
        halt(0).
    """
    replacements = {
        "BOOT_ARGS": json.dumps([] if assets.boot is None else ["-boot", str(assets.boot)]),
        "EXECUTABLE": json.dumps(str(assets.executable)),
        "DIRECTORY": json.dumps(str(tmp_path)),
        "PYTHON_PATHS": json.dumps([str(ROOT / "src"), str(ROOT), *site.getsitepackages()]),
    }
    for key, value in replacements.items():
        expression = expression.replace(key, value)
    argv = [
        str(assets.executable),
        "+S",
        "2:2",
        "-noshell",
        "-name",
        "actor_state_controller@127.0.0.1",
    ]
    if assets.boot is not None:
        argv.extend(("-boot", str(assets.boot)))
    argv.extend(("-pa", *(str(path) for path in assets.code_paths), "-eval", expression))
    result = subprocess.run(
        argv,
        cwd=tmp_path,
        env=dict(os.environ, **assets.environment),
        capture_output=True,
        text=True,
        timeout=110,
        check=False,
    )
    (tmp_path / "controller.stdout").write_text(result.stdout)
    (tmp_path / "controller.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACTOR_CLUSTER_OK count=5 owners=1 standby=1 clean_stop" in result.stdout


@pytest.mark.runtime
def test_acknowledged_state_survives_sigkill_of_whole_vm(
    assets: RuntimeAssets, tmp_path: Path
) -> None:
    environment = dict(os.environ, **assets.environment)
    with subprocess.Popen(
        command(assets, tmp_path, "prepare_crash", wait=True),
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as process:
        assert process.stdout is not None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                output = b""
                while b"actor-state-ready" not in output:
                    assert selector.select(timeout=40), output.decode(errors="replace")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    assert chunk, output.decode(errors="replace")
                    output += chunk
        finally:
            process.kill()
            process.wait(timeout=10)
    result = subprocess.run(
        command(assets, tmp_path, "verify_crash"),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "actor-state-ok" in result.stdout
