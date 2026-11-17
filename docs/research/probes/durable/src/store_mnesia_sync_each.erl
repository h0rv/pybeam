-module(store_mnesia_sync_each).
-export([start/1, setup/1, put/2, get/1, change/3, leader/0]).
start(Dir) -> store_mnesia:start(Dir).
setup(Nodes) -> store_mnesia:setup(Nodes, false).
put(Id, Job) -> ok = store_mnesia:put(Id, Job), barrier().
get(Id) -> store_mnesia:get(Id).
change(Id, Expected, New) ->
    case store_mnesia:change(Id, Expected, New) of
        {ok, changed} -> ok = barrier(), {ok, changed};
        Other -> Other
    end.
leader() -> node().
barrier() ->
    Nodes = mnesia:table_info(jobs, where_to_write),
    true = lists:member(node(), Nodes),
    {Replies, []} = rpc:multicall(Nodes, mnesia, sync_log, [], 60000),
    true = lists:all(fun(R) -> R =:= ok end, Replies), ok.
