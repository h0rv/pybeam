#!/usr/bin/env escript
%%! +S 2:2

main([Root, Runtime, "--python-path" | PythonPaths]) ->
    true = code:add_patha(filename:join(Runtime, "ebin")),
    ok = application:load(erlang_python),
    ok = application:set_env(erlang_python, num_contexts, 1),
    ok = application:set_env(erlang_python, event_loop_pool_size, 1),
    {ok, _} = application:ensure_all_started(erlang_python),
    ok = py_import:add_paths(PythonPaths),
    {ok, Worker} = py_context:new(#{mode => worker}),
    try
        ok = py_context:exec(Worker, iolist_to_binary(io_lib:format(
            "import sys; sys.path[:0] = [~p, ~p]", [Root, filename:join(Root, "src")]))),
        ok = py_context:exec(Worker, <<"import tests.native.service_objects">>),
        ok = py_context:start_loop(Worker),
        io:format("Native submit has no mailbox policy; ServiceSlot bounds local tasks only.~n"),
        case py_context:submit_await(Worker, 'tests.native.service_objects', run, [], #{}, 10000) of
            {ok, Outcome = {<<"completed">>, <<"failed">>, <<"cancelled">>, <<"x">>, true, 4, 0}} ->
                ok = py_context:stop_loop(Worker, 1000),
                receive
                    {py_loop_exit, Worker, _} -> ok
                after 5000 ->
                    error(missing_loop_exit)
                end,
                Monitor = erlang:monitor(process, Worker),
                ok = py_context:stop(Worker),
                receive
                    {'DOWN', Monitor, process, Worker, _} -> ok
                after 5000 ->
                    error(context_not_stopped)
                end,
                false = lists:keymember(hornbeam, 1, application:which_applications()),
                ok = application:stop(erlang_python),
                io:format("services-example-ok ~p~n", [Outcome]);
            Other ->
                error({service_cleanup_not_acknowledged, Other})
        end
    catch
        Class:Reason:Stack ->
            io:format(standard_error,
                "Service cleanup is not acknowledged. VM teardown is not service cleanup.~n~p~n",
                [{Class, Reason, Stack}]),
            halt(1)
    end;
main(_Args) ->
    io:format(standard_error,
        "No launch options are supported. Native bounded mailbox admission is unavailable.~n", []),
    halt(2).
