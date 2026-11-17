-module(store_mnesia).
-export([start/1, setup/1, put/2, get/1, change/3, leader/0]).
start(Dir) ->
    application:set_env(mnesia, dir, Dir),
    mnesia:create_schema([node()]),
    ok = application:start(mnesia), ok.
setup([First | Rest] = Nodes) ->
    [begin
         stopped = rpc:call(N, mnesia, stop, []),
         ok = rpc:call(N, mnesia, delete_schema, [[N]]),
         ok = rpc:call(N, application, start, [mnesia]),
         {ok, _} = rpc:call(N, mnesia, change_config, [extra_db_nodes, [First]]),
         {atomic, ok} = rpc:call(First, mnesia, change_table_copy_type, [schema, N, disc_copies])
     end || N <- Rest],
    {atomic, ok} = rpc:call(First, mnesia, create_table,
                          [jobs, [{attributes, [id, value]}, {disc_copies, Nodes}, {majority, true}]]),
    [ok = rpc:call(N, mnesia, wait_for_tables, [[jobs], 30000]) || N <- Nodes], ok.
put(Id, Job) -> {atomic, ok} = mnesia:sync_transaction(fun() -> mnesia:write({jobs, Id, Job}) end), ok.
get(Id) -> case mnesia:dirty_read(jobs, Id) of [{jobs, Id, J}] -> {ok, J}; [] -> missing end.
change(Id, Expected, New) ->
    case mnesia:sync_transaction(fun() ->
        case mnesia:read(jobs, Id, write) of
            [{jobs, Id, Expected}] -> mnesia:write({jobs, Id, New}), changed;
            _ -> unchanged
        end
    end) of {atomic, R} -> {ok, R}; Other -> Other end.
leader() -> node().
