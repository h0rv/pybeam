-module(mnesia_group_commit).
-behaviour(gen_server).
-export([start/0, sync/0, stats/0, init/1, handle_call/3, handle_cast/2, handle_info/2]).

start() -> gen_server:start({local, ?MODULE}, ?MODULE, [], []).
sync() -> gen_server:call(?MODULE, sync, infinity).
stats() -> gen_server:call(?MODULE, stats).
init([]) -> {ok, {[], 0, 0, 0}}.

handle_call(sync, From, {Waiters, Batches, Calls, Max}) ->
    case Waiters of [] -> erlang:send_after(1, self(), flush); _ -> ok end,
    {noreply, {[From | Waiters], Batches, Calls, Max}};
handle_call(stats, _, {Waiters, Batches, Calls, Max} = State) ->
    {reply, #{batches => Batches, callers => Calls, max_batch => Max,
              waiting => length(Waiters)}, State}.
handle_cast(_, State) -> {noreply, State}.
handle_info(flush, {Waiters, Batches, Calls, Max}) ->
    Result = mnesia:sync_log(),
    [gen_server:reply(From, Result) || From <- Waiters],
    Size = length(Waiters),
    {noreply, {[], Batches + 1, Calls + Size, max(Max, Size)}}.
