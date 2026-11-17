-module(store_khepri).
-export([start/1, setup/1, put/2, get/1, change/3, leader/0]).
start(Dir) -> {ok, jobs} = khepri:start(Dir, jobs), ok.
setup([First | Rest]) ->
    [ok = rpc:call(N, khepri_cluster, join, [jobs, First]) || N <- Rest], ok.
put(Id, Job) -> khepri:put(jobs, [jobs, integer_to_binary(Id)], Job).
get(Id) -> khepri:get(jobs, [jobs, integer_to_binary(Id)], #{favor => consistency}).
change(Id, Expected, New) ->
    Path = [jobs, integer_to_binary(Id)],
    khepri:transaction(jobs, fun() ->
        case khepri_tx:get(Path) of
            {ok, Expected} -> ok = khepri_tx:put(Path, New), changed;
            _ -> unchanged
        end
    end, rw).
leader() ->
    case ra:members({jobs, node()}) of
        {ok, _, {jobs, N}} -> N;
        _ -> timer:sleep(10), leader()
    end.
