-module(pybeam_genstage_probe).
-behaviour(application).
-behaviour(supervisor).
-export([start/2, stop/1, init/1]).

start(_, _) ->
    ok = py:register_function(genstage_owned_probe, fun([]) ->
        'Elixir.PyBEAM.GenStageProbe':verify()
    end),
    supervisor:start_link(?MODULE, []).

stop(_) -> py:unregister_function(genstage_owned_probe).
init([]) -> {ok, {#{strategy => one_for_one, intensity => 0, period => 1}, []}}.
