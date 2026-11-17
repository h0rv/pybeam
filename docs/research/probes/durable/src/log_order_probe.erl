-module(log_order_probe).
-export([run/1]).

run(Mode) ->
    Root = filename:absname("log-order-" ++ atom_to_list(Mode) ++ "-" ++
                            integer_to_list(erlang:system_time(millisecond))),
    ok = filelib:ensure_dir(filename:join(Root, "unused")),
    Names = case Mode of
        single -> [logproof1];
        symmetric -> [logproof1, logproof2, logproof3];
        majority -> [logproof1, logproof2, logproof3]
    end,
    Nodes = start_nodes(Names, Root),
    try
        ok = store_mnesia:setup(Nodes, Mode =:= majority),
        [ok = rpc:call(N, sys, suspend, [mnesia_recover]) || N <- Nodes],
        [First | _] = Nodes,
        Job = #{id => 1, state => queued, attempts => 0, payload => <<0:256>>},
        Put = rpc:call(First, store_mnesia, put, [1, Job]),
        io:format("ORDER mode=~p put=~p~n", [Mode, Put]),
        [begin
             Sync = rpc:call(N, mnesia, sync_log, []),
             Pid = rpc:call(N, erlang, whereis, [mnesia_recover]),
             {messages, Messages} = rpc:call(N, erlang, process_info, [Pid, messages]),
             Pending = length([D || {'$gen_cast', {log_decision, D}} <- Messages,
                                     element(3, D) =:= committed]),
             io:format("ORDER node=~p sync_log=~p pending_committed_decisions=~p~n",
                       [N, Sync, Pending])
         end || N <- Nodes],
        [kill(N) || N <- Nodes],
        [wait_down(N, 100) || N <- Nodes],
        Recovered = start_nodes(Names, Root),
        [begin
             Ready = rpc:call(N, mnesia, wait_for_tables, [[jobs], 30000], 35000),
             Read = rpc:call(N, store_mnesia, get, [1]),
             io:format("ORDER_RECOVERED mode=~p node=~p ready=~p job=~p~n",
                       [Mode, N, Ready, Read])
         end || N <- Recovered],
        io:format("ORDER_ROOT ~s~n", [Root])
    after
        [case net_adm:ping(N) of pong -> kill(N); pang -> ok end || N <- Nodes]
    end.

start_nodes(Names, Root) ->
    Nodes = [start_peer(Name) || Name <- Names],
    Starts = [rpc:async_call(N, store_mnesia, start,
                            [filename:join(Root, atom_to_list(Name))])
              || {N, Name} <- lists:zip(Nodes, Names)],
    [ok = rpc:yield(Start) || Start <- Starts],
    Nodes.

start_peer(Name) ->
    Ebin = filename:dirname(filename:absname(code:which(store_mnesia))),
    {ok, _, N} = peer:start(#{name => Name,
                             args => ["+S", "2:2", "+A", "2", "-setcookie",
                                      atom_to_list(erlang:get_cookie()), "-pa", Ebin]}),
    ok = rpc:call(N, logger, set_primary_config, [level, error]),
    N.

kill(N) ->
    Pid = rpc:call(N, os, getpid, []),
    os:cmd("kill -9 " ++ Pid),
    ok.

wait_down(_, 0) -> error(node_still_alive);
wait_down(N, Left) ->
    case net_adm:ping(N) of
        pang -> ok;
        pong -> timer:sleep(20), wait_down(N, Left - 1)
    end.
