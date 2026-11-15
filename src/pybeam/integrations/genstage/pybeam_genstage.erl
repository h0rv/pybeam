-module(pybeam_genstage).
-behaviour(application).
-behaviour(supervisor).
-export([start/2, stop/1, init/1]).

start(_Type, _Args) ->
    ok = 'Elixir.PyBEAM.GenStage.Bridge':install(),
    supervisor:start_link(?MODULE, []).

stop(_State) ->
    py:unregister_function(pybeam_genstage).

init([]) ->
    {ok, {#{strategy => one_for_one, intensity => 0, period => 1}, []}}.
