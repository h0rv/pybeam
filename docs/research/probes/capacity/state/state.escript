#!/usr/bin/env escript
%%! -noshell
-mode(compile).

main([App, Dir]) ->
    true = code:add_patha(filename:join(App, "ebin")),
    application:load(erlang_python),
    application:set_env(erlang_python, num_contexts, 1),
    {ok, _} = application:ensure_all_started(erlang_python),
    {ok, Ctx} = py_context:new(#{mode => worker}),
    ok = done(py_context:exec(Ctx, <<"import sys\nsys.path.insert(0, '", (list_to_binary(Dir))/binary, "')\n">>)),
    Table = ets:new(state, [set, public, {read_concurrency, true}]),
    [measure(Ctx, Table, Entries) || Entries <- [32, 3300, 33000]],
    halt(0).

done(ok) -> ok;
done({ok, _}) -> ok.

call(Ctx, Func, Args) -> py_context:call(Ctx, state_job, Func, Args, #{}, 60000).

measure(Ctx, Table, Entries) ->
    {ok, Blob} = call(Ctx, build, [Entries]),
    true = ets:insert(Table, {actor, Blob}),
    Pinned = time(fun() -> {ok, _} = call(Ctx, step_pinned, []) end),
    Transfer = time(fun() -> [{actor, B}] = ets:lookup(Table, actor), {ok, _} = call(Ctx, step_transfer, [B]) end),
    Copy = time(fun() ->
        [{actor, B}] = ets:lookup(Table, actor),
        {ok, Next} = call(Ctx, step_copy, [B]),
        true = ets:insert(Table, {actor, Next})
    end),
    Lookup = time(fun() -> [{actor, _}] = ets:lookup(Table, actor) end),
    io:format("~ts~n", [json:encode(#{bytes => byte_size(Blob), pinned_us => Pinned,
        ets_lookup_us => Lookup, transfer_us => Transfer, copy_us => Copy})]).

time(Fun) ->
    Fun(),
    Samples = lists:sort([begin B = erlang:monotonic_time(nanosecond), Fun(),
                          erlang:monotonic_time(nanosecond) - B end || _ <- lists:seq(1, 300)]),
    round(lists:nth(150, Samples) / 100) / 10.
