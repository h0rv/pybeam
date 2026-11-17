#!/usr/bin/env escript
%%! +P 2000000 -noshell
-mode(compile).

main([App, JobDir, Kind, Mode, Count, Size]) ->
    try
        true = code:add_patha(filename:join(App, "ebin")),
        process_flag(trap_exit, true),
        Result = run(list_to_atom(Kind), list_to_atom(Mode), list_to_integer(Count),
                     list_to_integer(Size), list_to_binary(JobDir), App),
        emit(Result#{vm_pid => list_to_integer(os:getpid()),
                     schedulers => erlang:system_info(schedulers_online)}),
        halt(0)
    catch Class:Reason:Stack ->
        emit(#{error => iolist_to_binary(io_lib:format("~p:~0p ~0p", [Class, Reason, hd(Stack)]))}),
        halt(2)
    end.

emit(Map) -> io:format("~ts~n", [json:encode(Map)]).

start(JobDir) ->
    application:load(erlang_python),
    application:set_env(erlang_python, num_contexts, 1),
    {ok, _} = application:ensure_all_started(erlang_python),
    JobDir.

path_code(JobDir) -> <<"import sys\nsys.path.insert(0, '", JobDir/binary, "')\n">>.

context(Mode, JobDir) ->
    Begin = now_us(),
    {ok, Ctx} = py_context:new(#{mode => Mode}),
    Created = now_us() - Begin,
    ok = done(py_context:exec(Ctx, path_code(JobDir))),
    {ok, 0} = py_context:call(Ctx, job, noop, []),
    {Ctx, Created}.

peer_node(App, JobDir) ->
    Begin = now_us(),
    {ok, Peer, _} = peer:start_link(#{connection => 0,
                                      args => ["-pa", filename:join(App, "ebin")]}),
    ok = peer:call(Peer, application, load, [erlang_python]),
    ok = peer:call(Peer, application, set_env, [erlang_python, num_contexts, 1]),
    {ok, _} = peer:call(Peer, application, ensure_all_started, [erlang_python]),
    Created = now_us() - Begin,
    ok = done(peer:call(Peer, py, exec, [path_code(JobDir)])),
    {Peer, Created}.

done(ok) -> ok;
done({ok, _}) -> ok.

call({peer, Peer}, Func, Args) -> peer:call(Peer, py, call, [job, Func, Args], 120000);
call(Ctx, Func, Args) -> py_context:call(Ctx, job, Func, Args, #{}, 120000).

attempt(Ctx, Func, Args) ->
    try call(Ctx, Func, Args) catch Class:Reason -> {Class, Reason} end.

workers(nodes, Count, App, JobDir) ->
    [begin {P, T} = peer_node(App, JobDir), {{peer, P}, T} end || _ <- lists:seq(1, Count)];
workers(Mode, Count, _App, JobDir) ->
    [context(Mode, JobDir) || _ <- lists:seq(1, Count)].

run(fanout, Mode, Count, Size, JobDir, App) ->
    start(JobDir),
    Base = rss_tree(),
    Pairs = workers(Mode, Count, App, JobDir),
    Rss = rss_tree() - Base,
    Ctxs = [C || {C, _} <- Pairs],
    [First | _] = Ctxs,
    Latency = lists:sort([begin B = now_us(), {ok, 0} = call(First, noop, []), now_us() - B end
                          || _ <- lists:seq(1, 200)]),
    {ok, Single} = call(First, spin, [Size]),
    Parent = self(),
    Begin = now_us(),
    Pids = [spawn_link(fun() -> Parent ! {span, self(), call(C, spin, [Size])} end) || C <- Ctxs],
    Spans = [receive {span, P, {ok, Span}} -> Span end || P <- Pids],
    Wall = now_us() - Begin,
    #{kind => fanout, mode => Mode, count => Count, size => Size,
      create_us => [T || {_, T} <- Pairs], rss_kib => Rss, base_rss_kib => Base,
      latency_p50_us => pct(Latency, 50), latency_p99_us => pct(Latency, 99),
      single_ns => maps:get(<<"end">>, Single) - maps:get(<<"start">>, Single),
      spans => Spans, wall_us => Wall};
run(crash, Mode, Count, _Size, JobDir, App) ->
    start(JobDir),
    Ctxs = [C || {C, _} <- workers(Mode, Count, App, JobDir)],
    [Victim | Others] = Ctxs,
    Outcome = attempt(Victim, crash, []),
    timer:sleep(200),
    Alive = length([C || C <- Others, alive(C)]),
    Begin = now_us(),
    [{_, _}] = workers(Mode, 1, App, JobDir),
    #{kind => crash, mode => Mode, count => Count, others_alive => Alive,
      victim_result => iolist_to_binary(io_lib:format("~0P", [Outcome, 8])),
      replacement_us => now_us() - Begin};
run(fanin, Mode, Count, Contexts, JobDir, App) ->
    start(JobDir),
    Ctxs = list_to_tuple([C || {C, _} <- workers(Mode, Contexts, App, JobDir)]),
    Parent = self(),
    erlang:garbage_collect(),
    Memory = erlang:memory(processes),
    Spawned = now_us(),
    Pids = [spawn(fun() ->
                receive go -> ok end,
                {ok, 0} = call(element(I rem Contexts + 1, Ctxs), noop, []),
                Parent ! done
            end) || I <- lists:seq(1, Count)],
    SpawnUs = now_us() - Spawned,
    PerProcess = (erlang:memory(processes) - Memory) div Count,
    Window = Contexts * 4,
    {First, Rest} = lists:split(min(Window, Count), Pids),
    Begin = now_us(),
    [P ! go || P <- First],
    admit(Rest, length(Pids)),
    Wall = now_us() - Begin,
    #{kind => fanin, mode => Mode, count => Count, contexts => Contexts, window => Window,
      spawn_us => SpawnUs, bytes_per_process => PerProcess, wall_us => Wall,
      rss_kib => rss_tree()}.

admit(_Waiting, 0) -> ok;
admit(Waiting, Running) ->
    receive done ->
        case Waiting of
            [Next | Rest] -> Next ! go, admit(Rest, Running - 1);
            [] -> admit([], Running - 1)
        end
    end.

alive(Ctx) ->
    case attempt(Ctx, noop, []) of
        {ok, 0} -> true;
        _ -> false
    end.

pct(Sorted, P) -> lists:nth(max(1, (length(Sorted) * P + 99) div 100), Sorted).

now_us() -> erlang:monotonic_time(microsecond).

rss_tree() ->
    Self = list_to_integer(os:getpid()),
    Table = [{Pid, Parent} || Pid <- pids(), Parent <- [ppid(Pid)], Parent =/= undefined],
    lists:sum([rss(Pid) || Pid <- descendants([Self], Table, [Self])]).

descendants([], _Table, Acc) -> Acc;
descendants(Front, Table, Acc) ->
    Next = [Pid || {Pid, Parent} <- Table, lists:member(Parent, Front)],
    descendants(Next, Table, Next ++ Acc).

pids() ->
    {ok, Names} = file:list_dir("/proc"),
    [list_to_integer(N) || N <- Names, lists:all(fun(C) -> C >= $0 andalso C =< $9 end, N)].

ppid(Pid) ->
    case file:read_file("/proc/" ++ integer_to_list(Pid) ++ "/stat") of
        {ok, Stat} ->
            [_, Rest] = binary:split(Stat, <<") ">>),
            [_, Parent | _] = binary:split(Rest, <<" ">>, [global]),
            binary_to_integer(Parent);
        _ -> undefined
    end.

rss(Pid) ->
    case file:read_file("/proc/" ++ integer_to_list(Pid) ++ "/status") of
        {ok, Status} ->
            case re:run(Status, <<"VmRSS:\\s+(\\d+)">>, [{capture, all_but_first, binary}]) of
                {match, [Kib]} -> binary_to_integer(Kib);
                nomatch -> 0
            end;
        _ -> 0
    end.
