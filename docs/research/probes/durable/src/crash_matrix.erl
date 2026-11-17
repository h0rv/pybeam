-module(crash_matrix).
-export([run/3, worker/5, ack/2, execution/1, group_check/0]).
-define(COUNT, 10000).

now_ms() -> erlang:monotonic_time(millisecond).
rank(J) -> {maps:get(epoch, J), maps:get(attempts, J),
            case maps:get(state, J) of queued -> 0; {claimed, _, _} -> 1; done -> 2 end}.
ack(Id, Job) -> ets:insert(oracle, {{Id, rank(Job)}, Job}), ok.
execution(Id) -> ets:insert(executions, {make_ref(), Id}), ok.
oracle() ->
    lists:foldl(fun({{Id, R}, J}, Acc) ->
        case maps:find(Id, Acc) of
            {ok, OldJ} -> case R > rank(OldJ) of true -> Acc#{Id => J}; false -> Acc end;
            error -> Acc#{Id => J}
        end
    end, #{}, ets:tab2list(oracle)).

run(Store, Replicas, Repeats) ->
    logger:set_primary_config(level, error),
    ets:new(oracle, [named_table, public, ordered_set]),
    ets:new(executions, [named_table, public, set]),
    Root = filename:absname(atom_to_list(Store) ++ "-r" ++ integer_to_list(Replicas) ++
                            "-matrix-" ++ integer_to_list(erlang:system_time(millisecond))),
    ok = filelib:ensure_dir(Root ++ "/unused"),
    Names = [list_to_atom("matrix" ++ integer_to_list(I)) || I <- lists:seq(1, Replicas)],
    Nodes = start_nodes(Store, Names, Root),
    try
        case Store of
            store_mnesia -> ok = store_mnesia:setup(Nodes, false);
            _ -> ok = Store:setup(Nodes)
        end,
        [First | _] = Nodes,
        io:format("MATRIX store=~p replicas=~p mnesia_majority=false count=~p root=~s~n",
                  [Store, Replicas, ?COUNT, Root]),
        enqueue(Store, First, -1, 1),
        enqueue(Store, First, 0, 16),
        clear_events(),
        T = now_ms(), Ws = workers(Store, Nodes),
        await_done(Store, First, now_ms() + 240000), stop_workers(Ws),
        io:format("WORK store=~p replicas=~p jobs_s=~.2f duplicates=~p~n",
                  [Store, Replicas, ?COUNT * 1000 / max(1, now_ms()-T), duplicates()]),
        stats(Store, Nodes),
        case Replicas of
            1 -> ok;
            _ -> single(Store, Nodes, Names, Root)
        end,
        [full(Store, Nodes, Names, Root, Trial) || Trial <- lists:seq(1, Repeats)],
        io:format("MATRIX_DONE store=~p replicas=~p full_trials=~p~n", [Store, Replicas, Repeats])
    after
        [case net_adm:ping(N) of pong -> kill_nodes([N]); pang -> ok end || N <- Nodes],
        ets:delete(oracle), ets:delete(executions)
    end.

enqueue(Store, Node, Epoch, Concurrency) ->
    Parent = self(), T = now_ms(),
    [spawn_link(fun() ->
        [begin
             Id = case Epoch of -1 -> -I; _ -> I end,
             J = #{id => Id, payload => <<0:256>>, epoch => Epoch, state => queued, attempts => 0},
             ok = rpc:call(Node, Store, put, [Id, J], 60000), ack(Id, J)
         end || I <- lists:seq(K, ?COUNT, Concurrency)],
        Parent ! enqueued
     end) || K <- lists:seq(1, Concurrency)],
    [receive enqueued -> ok after 180000 -> error(enqueue_timeout) end || _ <- lists:seq(1, Concurrency)],
    io:format("ENQUEUE store=~p epoch=~p concurrency=~p jobs_s=~.2f~n",
              [Store, Epoch, Concurrency, ?COUNT * 1000 / max(1, now_ms()-T)]).

worker(Store, Controller, Count, Offset, Step) ->
    Id = (Offset + Step) rem Count + 1,
    try
        {ok, J} = Store:get(Id),
        Now = erlang:system_time(millisecond),
        Can = case maps:get(state, J) of
            queued -> true;
            {claimed, _, Until} -> Until < Now;
            done -> false
        end,
        case Can of
            true ->
                Claim = J#{state => {claimed, node(), Now+2000}, attempts => maps:get(attempts,J)+1},
                case Store:change(Id, J, Claim) of
                    {ok, changed} ->
                        ok = rpc:call(Controller, ?MODULE, ack, [Id, Claim]),
                        ok = rpc:call(Controller, ?MODULE, execution, [Id]),
                        timer:sleep(1 + Id rem 5),
                        Done = Claim#{state => done},
                        case Store:change(Id, Claim, Done) of
                            {ok, changed} -> ok = rpc:call(Controller, ?MODULE, ack, [Id, Done]);
                            _ -> ok
                        end;
                    _ -> ok
                end;
            false -> ok
        end
    catch _:_ -> timer:sleep(10) end,
    worker(Store, Controller, Count, Offset, Step+1).

workers(Store, Nodes) ->
    [spawn(lists:nth(K rem length(Nodes)+1, Nodes), ?MODULE, worker,
           [Store, node(), ?COUNT, K*811, 0]) || K <- lists:seq(0, 11)].
stop_workers(Ws) -> [exit(P, kill) || P <- Ws], timer:sleep(50).
clear_events() -> ets:delete_all_objects(oracle), ets:delete_all_objects(executions).
done_count() -> length([ok || {{Id, {_, _, 2}}, _} <- ets:tab2list(oracle), Id > 0]).
await_progress(Deadline) ->
    case done_count() >= 1000 of
        true -> ok;
        false ->
            true = now_ms() < Deadline,
            timer:sleep(10), await_progress(Deadline)
    end.
await_done(Store, Node, Deadline) ->
    case done_count() >= ?COUNT of
        true -> ok;
        false ->
            case done_count() > ?COUNT-24 of
                true ->
                    [case rpc:call(Node, Store, get, [Id]) of
                         {ok, #{state := done} = J} -> ack(Id, J);
                         _ -> ok
                     end || Id <- lists:seq(1, ?COUNT)];
                false -> ok
            end,
            case now_ms() < Deadline of true -> timer:sleep(50), await_done(Store, Node, Deadline);
                false -> error({work_timeout, done_count()}) end
    end.
duplicates() ->
    Counts = lists:foldl(fun({_, Id}, Acc) -> maps:update_with(Id, fun(N)->N+1 end, 1, Acc) end,
                        #{}, ets:tab2list(executions)),
    length([ok || {_, C} <- maps:to_list(Counts), C > 1]).

single(Store, Nodes, Names, Root) ->
    [First | _] = Nodes,
    clear_events(), enqueue(Store, First, 1, 16),
    T = now_ms(), Ws = workers(Store, Nodes), await_progress(now_ms()+60000),
    Dead = rpc:call(First, Store, leader, []),
    kill_nodes([Dead]),
    [Live | _] = Nodes -- [Dead],
    report(single_cut, Store, length(Nodes), 1, Nodes -- [Dead], oracle()),
    Short = lists:nth(index(Dead, Nodes), Names),
    [Dead] = start_nodes(Store, [Short], Root), ready(Store, Dead),
    await_done(Store, Live, now_ms()+240000), stop_workers(Ws),
    report(single, Store, length(Nodes), 1, Nodes, oracle()),
    io:format("SINGLE_WORK store=~p replicas=~p jobs_s=~.2f duplicates=~p rejoined=true~n",
              [Store,length(Nodes),?COUNT*1000/max(1,now_ms()-T),duplicates()]).
index(X, [X | _]) -> 1;
index(X, [_ | Tail]) -> 1 + index(X, Tail).

full(Store, Nodes, Names, Root, Trial) ->
    [First | _] = Nodes,
    clear_events(), enqueue(Store, First, Trial+1, 16),
    Ws = workers(Store, Nodes), await_progress(now_ms()+60000),
    Alive = length([P || P <- Ws, rpc:call(node(P), erlang, is_process_alive, [P]) =:= true]),
    io:format("ACTIVE store=~p replicas=~p trial=~p workers_alive=~p acknowledged_done=~p~n",
              [Store,length(Nodes),Trial,Alive,done_count()]),
    kill_nodes(Nodes),
    Before = oracle(),
    Nodes = start_nodes(Store, Names, Root),
    [ready(Store,N) || N <- Nodes],
    {Missing, Recovered} = report(full, Store, length(Nodes), Trial, Nodes, Before),
    case Missing of
        0 ->
            ets:delete_all_objects(oracle),
            [ack(I,J) || {I,{ok,J}} <- maps:to_list(Recovered)],
            RecoveryWorkers = workers(Store, Nodes),
            await_done(Store, First, now_ms()+240000), stop_workers(RecoveryWorkers),
            io:format("RECOVERY store=~p replicas=~p trial=~p complete=true duplicates=~p~n",
                      [Store,length(Nodes),Trial,duplicates()]);
        _ -> io:format("RECOVERY store=~p replicas=~p trial=~p complete=false duplicates=~p reason=missing_jobs~n",
                       [Store,length(Nodes),Trial,duplicates()])
    end.

report(Kind, Store, Replicas, Trial, Nodes, Before) ->
    Counts = [begin
        After = maps:from_list([{I,rpc:call(N,Store,get,[I],60000)} || I <- lists:seq(1,?COUNT)]),
        Missing = [I || {I,V} <- maps:to_list(After), V =:= missing],
        Errors = [{I,V} || {I,V} <- maps:to_list(After), V =/= missing,
                          not (is_tuple(V) andalso element(1,V) =:= ok)],
        [] = Errors,
        Regressions = [I || {I,J} <- maps:to_list(Before), I > 0,
                          {ok,A} <- [maps:get(I,After)],
                          (maps:get(epoch,A) =/= maps:get(epoch,J)) orelse rank(A) < rank(J)
                          orelse (rank(A) =:= rank(J) andalso A =/= J)],
        io:format("CRASH kind=~p store=~p replicas=~p trial=~p node=~p acked=~p lost=~p regressions=~p examples=~p~n",
                  [Kind,Store,Replicas,Trial,N,map_size(Before),length(Missing),length(Regressions),
                   lists:sublist(Regressions,5)]),
        ok = file:write_file(filename:absname(
             atom_to_list(Store)++"-r"++integer_to_list(Replicas)++"-"++atom_to_list(Kind)++
             "-"++integer_to_list(Trial)++"-"++atom_to_list(N)++".term"),term_to_binary({Before,After})),
        {length(Missing),After}
    end || N <- Nodes],
    {lists:max([C || {C,_} <- Counts]),element(2,hd(Counts))}.

start_nodes(Store, Names, Root) ->
    BuildRoot = filename:absname("_build") ++ "/",
    Paths = lists:append([["-pa",P] || Path <- code:get_path(), P <- [filename:absname(Path)],
                                     lists:prefix(BuildRoot,P)]),
    Nodes = [begin
        {ok,_,N} = peer:start(#{name=>Name,args=>["+S","2:2","+A","2","-setcookie",
                                              atom_to_list(erlang:get_cookie()) | Paths]}),
        ok = rpc:call(N, logger, set_primary_config, [level,error]), N
    end || Name <- Names],
    Starts = [rpc:async_call(N,Store,start,[filename:join(Root,atom_to_list(Name))])
              || {N,Name} <- lists:zip(Nodes,Names)],
    [begin {value,ok}=rpc:nb_yield(Key,60000) end || Key <- Starts], Nodes.
ready(store_khepri, N) -> {ok,_}=rpc:call(N,store_khepri,get,[1],60000), ok;
ready(_, N) -> ok=rpc:call(N,mnesia,wait_for_tables,[[jobs],60000],65000).
kill_nodes(Nodes) ->
    Pids = [rpc:call(N,os,getpid,[]) || N <- Nodes], Parent=self(),
    [spawn_link(fun()-> os:cmd("kill -9 "++Pid), Parent!killed end) || Pid <- Pids],
    [receive killed -> ok end || _ <- Pids],
    [wait_down(N,100) || N <- Nodes].
wait_down(_,0) -> error(node_still_up);
wait_down(N,Left) -> case net_adm:ping(N) of pang -> ok; pong -> timer:sleep(20),wait_down(N,Left-1) end.
stats(store_mnesia_synced,Nodes) ->
    [io:format("GROUP node=~p stats=~p~n",[N,rpc:call(N,mnesia_group_commit,stats,[])]) || N <- Nodes];
stats(_,_) -> ok.

group_check() ->
    {ok,_}=mnesia_group_commit:start(),
    #{batches:=0,callers:=0,waiting:=0}=mnesia_group_commit:stats(),
    Result=mnesia_group_commit:sync(),
    {error,_}=Result,
    io:format("GROUP_CHECK empty=ok stopped_mnesia_error=~p~n",[Result]),
    gen_server:stop(mnesia_group_commit), ok.
