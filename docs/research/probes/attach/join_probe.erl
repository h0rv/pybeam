-module(join_probe).
-export([run/0, worker_init/0, work/2, slow/1, push/1]).

worker_init() ->
    application:load(erlang_python),
    application:set_env(erlang_python, num_contexts, 2),
    application:set_env(erlang_python, event_loop_pool_size, 1),
    {ok, _} = application:ensure_all_started(erlang_python),
    Ctx = py:context(1),
    ok = py_context:exec(Ctx, <<"import os, sys; sys.path[:0] = os.environ['PYTHONPATH'].split(':')">>),
    ok = py_context:start_loop(Ctx).

work(Host, HostNode) ->
    persistent_term:put(probe_receiver, Host),
    ok = py:register_function(probe_push, ?MODULE, push),
    py_context:submit_await(py:context(1), join_work, work, [Host, HostNode], #{}, 10000).

push([Payload]) -> persistent_term:get(probe_receiver) ! Payload, ok.

slow(Host) ->
    py_context:submit_await(py:context(1), join_work, slow, [Host], #{}, 30000).

run() ->
    false = lists:keymember(erlang_python, 1, application:loaded_applications()),
    non_existing = code:which(py),
    io:format("host: no erlang_python application or py module~n"),
    true = register(probe_receiver, self()),
    Paths = string:split(os:getenv("PROBE_EBINS"), ":", all),
    Args = ["+S", "2:2", "+SDcpu", "2", "+SDio", "2", "-setcookie", "pybeam_probe", "-pa" | Paths],
    {ok, Peer, PyNode} = peer:start(#{name => pyworker, connection => standard_io, args => Args}),
    try
        ok = erpc:call(PyNode, ?MODULE, worker_init, []),
        io:format("worker connected=~p nodes=~p~n", [net_adm:ping(PyNode), nodes()]),
        show_work(PyNode),
        true = monitor_node(PyNode, true),
        Host = self(),
        spawn(fun() ->
            Outcome = try erpc:call(PyNode, ?MODULE, slow, [Host], 30000)
                      catch Class:Reason -> {Class, Reason} end,
            Host ! {slow_outcome, Outcome}
        end),
        receive <<"slow_started">> -> ok after 10000 -> error(slow_not_started) end,
        Pid = erpc:call(PyNode, os, getpid, []),
        [] = os:cmd("kill -KILL " ++ Pid),
        receive {slow_outcome, Outcome} -> io:format("killed worker mid-call: ~p~n", [Outcome])
        after 10000 -> error(no_rpc_failure) end,
        receive {nodedown, PyNode} -> io:format("monitor_node: nodedown~n")
        after 10000 -> error(no_nodedown) end,
        io:format("host survives worker kill: ~p~n", [is_process_alive(self())])
    after try peer:stop(Peer) catch _:_ -> ok end end,
    managed(Paths),
    false = lists:keymember(erlang_python, 1, application:loaded_applications()),
    io:format("host still has no erlang_python: true~n"),
    halt(0).

show_work(Node) ->
    Result = erpc:call(Node, ?MODULE, work, [self(), node()], 15000),
    io:format("remote async work: ~p~n", [Result]),
    {ok, #{<<"actors">> := <<"12 then 15; group closed">>,
           <<"tasks">> := <<"42; supervisor closed">>}} = Result,
    receive {<<"push">>, 42} -> io:format("host registered process received Python push via Erlang callback bridge: 42~n")
    after 5000 -> error(no_push) end.

managed(Paths) ->
    Python = os:getenv("PROBE_PYTHON"),
    Directory = os:getenv("PROBE_DIRECTORY"),
    Workdir = os:getenv("PROBE_WORKDIR"),
    Runtime = filename:join(os:getenv("PROBE_ROOT"), "_build/hornbeam"),
    Config = ["[tool.pybeam]\ndevelopment_runtime = ",
              json:encode(unicode:characters_to_binary(Runtime)), "\n"],
    ok = file:write_file(filename:join(Workdir, "pyproject.toml"), Config),
    Port = open_port({spawn_executable, Python}, [binary, exit_status, stderr_to_stdout,
        {line, 8192}, {cd, Workdir}, {args, [filename:join(Directory, "managed_wait.py")]},
        {env, [{"ERL_FLAGS", "+S 2:2 +SDcpu 2 +SDio 2 -sname managed -setcookie pybeam_probe"}]}]),
    try
        ready(Port),
        [_Name, Hostname] = string:split(atom_to_list(node()), "@"),
        Node = list_to_atom("managed@" ++ Hostname),
        pong = net_adm:ping(Node),
        io:format("managed pybeam.run VM ping=pong node=~p~n", [Node]),
        true = erpc:call(Node, code, add_patha, [hd(Paths)]),
        io:format("managed distribution environment=~p dist_auto_connect=~p~n", [
            erpc:call(Node, os, getenv, ["PYBEAM_DISTRIBUTION"]),
            erpc:call(Node, application, get_env, [kernel, dist_auto_connect])]),
        show_work(Node),
        {os_pid, Pid} = erlang:port_info(Port, os_pid),
        [] = os:cmd("kill -TERM " ++ integer_to_list(Pid)),
        finished(Port)
    after try port_close(Port) catch _:_ -> ok end end.

ready(Port) ->
    receive
        {Port, {data, {eol, <<"MANAGED_READY">>}}} -> ok;
        {Port, {data, {_, Line}}} -> io:format("managed: ~ts~n", [Line]), ready(Port);
        {Port, {exit_status, Status}} -> error({managed_early_exit, Status})
    after 40000 -> error(managed_start_timeout) end.

finished(Port) ->
    receive
        {Port, {data, {_, Line}}} -> io:format("managed: ~ts~n", [Line]), finished(Port);
        {Port, {exit_status, 143}} -> io:format("managed launcher exit after SIGTERM: 143~n");
        {Port, {exit_status, Status}} -> error({managed_stop_status, Status})
    after 15000 -> error(managed_stop_timeout) end.
