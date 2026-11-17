#!/usr/bin/env escript
%%! +P 2000000 -noshell
-mode(compile).

main([App, JobDir, Count]) ->
    true = code:add_patha(filename:join(App, "ebin")),
    application:load(erlang_python),
    application:set_env(erlang_python, num_contexts, 1),
    {ok, _} = application:ensure_all_started(erlang_python),
    N = list_to_integer(Count),
    Ctxs = list_to_tuple([begin
        {ok, C} = py_context:new(#{mode => worker}),
        ok = done(py_context:exec(C, <<"import sys\nsys.path.insert(0, '", (list_to_binary(JobDir))/binary, "')\n">>)),
        C end || _ <- lists:seq(1, 8)]),
    Parent = self(),
    Begin = erlang:monotonic_time(microsecond),
    [spawn(fun() ->
        R = try py_context:call(element(I rem 8 + 1, Ctxs), job, noop, [], #{}, 600000)
            catch C:E -> {C, E} end,
        Parent ! {done, R}
     end) || I <- lists:seq(1, N)],
    Results = [receive {done, R} -> R end || _ <- lists:seq(1, N)],
    Wall = erlang:monotonic_time(microsecond) - Begin,
    Ok = length([ok || {ok, 0} <- Results]),
    Errors = lists:usort([R || R <- Results, R =/= {ok, 0}]),
    io:format("~ts~n", [json:encode(#{callers => N, ok => Ok, wall_us => Wall,
        errors => iolist_to_binary(io_lib:format("~0P", [Errors, 6]))})]),
    halt(0).

done(ok) -> ok;
done({ok, _}) -> ok.
