-module(pybeam_genstage).
-export([bootstrap/0]).

bootstrap() ->
    application:load(erlang_python),
    application:set_env(erlang_python, num_contexts, 1),
    application:set_env(erlang_python, event_loop_pool_size, 1),
    {ok, _} = application:ensure_all_started(erlang_python),
    {ok, _} = application:ensure_all_started(elixir),
    {ok, _} = application:ensure_all_started(gen_stage),
    'Elixir.PyBEAM.GenStage.Bridge':install().
