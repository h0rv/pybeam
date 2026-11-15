#!/usr/bin/env escript
%%! +S 2:2 -noshell

main([Root, Runtime, "--python-path" | PythonPaths]) ->
    true = code:add_patha(filename:join(Runtime, "ebin")),
    {ok, py_supervision_demo, Binary} = compile:file(
        filename:join([Root, "tests", "native", "py_supervision_demo.erl"]),
        [binary, warnings_as_errors]),
    {module, py_supervision_demo} = code:load_binary(py_supervision_demo, "fixture", Binary),
    {ok, _} = application:ensure_all_started(erlang_python),
    ok = py_import:add_paths(PythonPaths),
    try run(Root)
    after application:stop(erlang_python) end.

run(Root) ->
    process_flag(trap_exit, true),
    ok = py_supervision_demo:register_callbacks(),
    {ok, Events} = py_channel:new(#{max_size => 4096}),
    try
        {ok, Parent} = py_supervision:start_link(#{
            strategy => one_for_one, intensity => 3, period => 5,
            mailbox => unbounded, max_children => unbounded}),
        Ref = monitor(process, Parent),
        try run_python(Root, Parent, Events)
        after
            ok = gen_server:stop(Parent, shutdown, 3000),
            receive {'DOWN', Ref, process, Parent, shutdown} ->
                io:format("root shutdown confirmed~n")
            after 1000 -> erlang:error(root_shutdown_not_confirmed) end
        end
    after py_channel:close(Events) end.

run_python(Root, Parent, Events) ->
    {ok, Context} = py_context:new(#{mode => worker}),
    try
        ok = py:exec(Context, iolist_to_binary(io_lib:format(
            "import sys; sys.path[:0] = [~p, ~p]",
            [Root, filename:join(Root, "src")]))),
        ok = py:exec(Context, <<"import tests.native.native_supervision">>),
        ok = py_context:start_loop(Context),
        case py_context:submit_await(
            Context, 'tests.native.native_supervision', main, [Parent, Events], #{}, 15000) of
            {ok, true} -> ok;
            Error -> erlang:error({fixture_failed, Error})
        end,
        ok = py_context:stop_loop(Context, 2000)
    after py_context:stop(Context) end.
