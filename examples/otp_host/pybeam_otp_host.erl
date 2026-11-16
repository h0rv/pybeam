-module(pybeam_otp_host).
-export([analyze/2]).

%% The host application owns erlang_python. This call owns only its context.
-spec analyze([binary()], binary()) -> {ok, term()} | {error, term()}.
analyze(PythonPaths, Json)
  when is_list(PythonPaths), is_binary(Json), byte_size(Json) =< 65536 ->
    {ok, Context} = py_context:new(#{
        mode => worker,
        managed_loop => dedicated,
        context_owner => self()
    }),
    try
        lists:foreach(fun(Path) ->
            {ok, _} = py_context:call(Context, site, addsitedir, [Path])
        end, PythonPaths),
        ok = py_context:start_loop(Context),
        try
            py_context:submit_await(
                Context, <<"examples.otp_host.library">>, <<"analyze">>,
                [Json], #{}, 5000)
        after
            ok = py_context:stop_loop(Context, 22000)
        end
    after
        ok = py_context:destroy(Context)
    end.
