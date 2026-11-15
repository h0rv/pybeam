-module(pybeam_genstage_probe).
-behaviour(application).
-behaviour(supervisor).
-export([start/2, stop/1, init/1]).

start(_, _) ->
    Gate = spawn_link(fun() -> gate(undefined, undefined) end),
    lists:foreach(fun({Name, Operation}) ->
        ok = py:register_function(Name, fun([]) ->
            Ref = make_ref(),
            Gate ! {Operation, self(), Ref},
            receive {Ref, ok} -> <<"ok">> after 2000 -> error(coexist_gate_timeout) end
        end)
    end, [{genstage_coexist_block, block}, {genstage_coexist_ready, ready},
          {genstage_coexist_release, release}]),
    ok = py:register_function(genstage_owned_probe, fun([]) ->
        'Elixir.PyBEAM.GenStageProbe':verify()
    end),
    supervisor:start_link(?MODULE, []).

gate(Blocked, Ready) ->
    receive
        {block, Pid, Ref} ->
            reply(Ready),
            gate({Pid, Ref}, Ready);
        {ready, Pid, Ref} ->
            case Blocked of undefined -> gate(Blocked, {Pid, Ref}); _ -> reply({Pid, Ref}), gate(Blocked, Ready) end;
        {release, Pid, Ref} ->
            reply(Blocked),
            reply({Pid, Ref})
    after 3000 -> exit(coexist_gate_timeout)
    end.

reply(undefined) -> ok;
reply({Pid, Ref}) -> Pid ! {Ref, ok}, ok.

stop(_) ->
    lists:foreach(fun py:unregister_function/1,
                  [genstage_owned_probe, genstage_coexist_block,
                   genstage_coexist_ready, genstage_coexist_release]).
init([]) -> {ok, {#{strategy => one_for_one, intensity => 0, period => 1}, []}}.
