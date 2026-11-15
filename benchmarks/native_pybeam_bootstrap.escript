#!/usr/bin/env escript
%%! +S 1:1 +SDcpu 1:1 +SDio 1 +A 1 +IOt 1 +IOp 1 +Bi
-mode(compile).

main([Root, Runtime, Trial]) ->
    try
        %% The broker must retain our process identity before any embedded work.
        await_ownership(filename:join(Trial, "spawn.reply.json"), 500),
        true = code:add_patha(filename:join(Runtime, "ebin")),
        {ok, Payload} = file:read_file(filename:join(Trial, "bootstrap.json")),
        true = os:putenv("PYBEAM_BOOTSTRAP", binary_to_list(Payload)),
        [true = os:putenv(Key, filename:join(Trial, Name)) || {Key, Name} <- [
            {"PYBEAM_SHUTDOWN", "shutdown"},
            {"PYBEAM_COMPLETION", "completed"},
            {"PYBEAM_CLEANUP", "cleanup"}
        ]],
        true = os:putenv("PYBEAM_SHUTDOWN_TIMEOUT_MS", "3000"),
        true = os:putenv("ERL_CRASH_DUMP", filename:join(Trial, "erl_crash.dump")),
        application:load(erlang_python),
        application:set_env(erlang_python, num_contexts, 1),
        application:set_env(erlang_python, event_loop_pool_size, 1),
        {ok, _} = application:ensure_all_started(erlang_python),
        %% os:putenv updates ERTS' environment, not embedded CPython's snapshot.
        {ok, _} = py_context:eval(py:context(1),
            <<"__import__('os').environ.__setitem__('PYBEAM_BOOTSTRAP', payload)">>,
            #{<<"payload">> => Payload}),
        ok = py_callback:register('_bench_inventory', fun([]) -> inventory(Runtime) end),
        %% Production loader and its cooperative shutdown run inside this owned VM.
        true = filelib:is_dir(filename:join(Root, "src/pybeam")),
        py_launch:start()
    catch Class:Reason:Stack ->
        io:format(standard_error, "Benchmark bootstrap failed ~p:~p~n~p~n",
                  [Class, Reason, Stack]),
        halt(1)
    end.

await_ownership(_Path, 0) -> error(guardian_ownership_timeout);
await_ownership(Path, Attempts) ->
    case filelib:is_file(Path) of
        true -> ok;
        false -> timer:sleep(10), await_ownership(Path, Attempts - 1)
    end.

inventory(Runtime) ->
    %% new/1 actor contexts are not children of py_context_sup. This existing
    %% registry covers every embedded context, including those owned by actors.
    Contexts = [P || {P, _} <- ets:tab2list(py_context_refs), is_process_alive(P)],
    Actors = [P || P <- processes(),
                   proc_lib:translate_initial_call(P) =:= {py_object_actor, init, 1}],
    Loaded = [unicode:characters_to_binary(Path) || {_, Path} <- code:all_loaded(),
        is_list(Path), lists:prefix(filename:join(Runtime, "ebin") ++ "/", Path)],
    OTPFile = filename:join([code:root_dir(), "releases",
                            erlang:system_info(otp_release), "OTP_VERSION"]),
    {ok, OTPVersion} = file:read_file(OTPFile),
    Beam = filename:join([code:root_dir(), "erts-" ++ erlang:system_info(version),
                         "bin", "beam.smp"]),
    iolist_to_binary(json:encode(#{
        otp => string:trim(OTPVersion),
        erts => unicode:characters_to_binary(erlang:system_info(system_version)),
        schedulers => erlang:system_info(schedulers_online),
        executable => unicode:characters_to_binary(Beam),
        contexts => [list_to_binary(pid_to_list(P)) || P <- lists:sort(Contexts)],
        actors => [list_to_binary(pid_to_list(P)) || P <- lists:sort(Actors)],
        loaded_beams => lists:sort(Loaded)
    })).
