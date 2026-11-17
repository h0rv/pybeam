#!/usr/bin/env escript
%%! +P 2000000 -noshell
-mode(compile).

main([App, JobDir]) ->
    true = code:add_patha(filename:join(App, "ebin")),
    application:load(erlang_python),
    application:set_env(erlang_python, num_contexts, 1),
    {ok, _} = application:ensure_all_started(erlang_python),
    put(dir, list_to_binary(JobDir)),
    [run(Max, Blocked) || Max <- [8, 64], Blocked <- [0, 8, 32]],
    halt(0).

new() ->
    {ok, C} = py_context:new(#{mode => worker}),
    _ = py_context:exec(C, <<"import sys\nsys.path.insert(0, '", (get(dir))/binary, "')\nimport job\n">>),
    C.

%% Pool: 8 warm contexts; grows to Max when all are busy.
pool(Free, Size, Max, Waiting, Peak) ->
    receive
        {checkout, From} when Free =/= [] ->
            [C | Rest] = Free, From ! {ctx, C}, pool(Rest, Size, Max, Waiting, Peak);
        {checkout, From} when Size < Max ->
            From ! {ctx, new()}, pool(Free, Size + 1, Max, Waiting, max(Peak, Size + 1));
        {checkout, From} ->
            pool(Free, Size, Max, Waiting ++ [From], Peak);
        {checkin, C} ->
            case Waiting of
                [W | Ws] -> W ! {ctx, C}, pool(Free, Size, Max, Ws, Peak);
                [] -> pool([C | Free], Size, Max, Waiting, Peak)
            end;
        {peak, From} -> From ! {peak, Peak}, pool(Free, Size, Max, Waiting, Peak)
    end.

call(Pool, Func, Args) ->
    Pool ! {checkout, self()},
    C = receive {ctx, X} -> X end,
    {ok, _} = py_context:call(C, job, Func, Args, #{}, 60000),
    Pool ! {checkin, C}.

run(Max, Blocked) ->
    Warm = [new() || _ <- lists:seq(1, 8)],
    Dir = get(dir),
    Pool = spawn(fun() -> put(dir, Dir), pool(Warm, 8, Max, [], 8) end),
    Parent = self(),
    [spawn(fun() -> call(Pool, spin, [200000]), Parent ! blocked end) || _ <- lists:seq(1, Blocked)],
    timer:sleep(20),
    Lat = [begin B = erlang:monotonic_time(microsecond), call(Pool, noop, []),
                 erlang:monotonic_time(microsecond) - B end || _ <- lists:seq(1, 200)],
    [receive blocked -> ok end || _ <- lists:seq(1, Blocked)],
    Pool ! {peak, self()},
    Peak = receive {peak, P} -> P end,
    S = lists:sort(Lat),
    io:format("~ts~n", [json:encode(#{max_workers => Max, blocked_200ms => Blocked, peak_workers => Peak,
        fast_p50_us => lists:nth(100, S), fast_p99_us => lists:nth(198, S), fast_max_us => lists:last(S)})]).
