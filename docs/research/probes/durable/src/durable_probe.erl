-module(durable_probe).
-export([boot/2, run/1, run/2, worker/4, event/2, enqueue/4, lease/1, hold_claim/2]).
boot(Store, Dir) ->
    logger:set_primary_config(level, error),
    ok = Store:start(Dir),
    receive stop -> ok end.
now_ms() -> erlang:system_time(millisecond).
event(Id, Kind) -> ets:insert(events, {{Id, Kind}, now_ms()}), ok.
launch(Store, N, Root) ->
    Short = hd(string:split(atom_to_list(N), "@")),
    Cmd = lists:flatten(io_lib:format("./run.sh node ~s ~s ~s/~s > ~s/~s.log 2>&1 &", [Short, Store, Root, Short, Root, Short])),
    os:cmd(Cmd), wait_node(N, 600).
wait_node(_, 0) -> error(node_timeout);
wait_node(N, Left) ->
    case rpc:call(N, erlang, whereis, [init]) of
        P when is_pid(P) -> timer:sleep(100), ok;
        _ -> timer:sleep(100), wait_node(N, Left - 1)
    end.
ready(Store, N, Left) when Left > 0 ->
    case rpc:call(N, Store, get, [1], 1000) of
        {ok, _} -> ok;
        _ -> timer:sleep(100), ready(Store, N, Left-1)
    end;
ready(_, _, 0) -> error(store_timeout).
kill(N) ->
    Pid = rpc:call(N, os, getpid, []),
    io:format("KILL ~p pid=~s time=~p~n", [N, Pid, now_ms()]),
    os:cmd("kill -9 " ++ Pid).
enqueue(Store, Node, Ids, Parent) ->
    [begin ok = rpc:call(Node, Store, put, [I, #{id => I, payload => <<0:256>>, state => queued, attempts => 0}], 60000),
           ets:insert(acks, {I}) end || I <- Ids],
    Parent ! enqueued.
produce(Store, N, Count, Concurrency) ->
    ets:delete_all_objects(acks),
    T = now_ms(), Parent = self(),
    Sign = case Concurrency of 1 -> -1; _ -> 1 end,
    [spawn_link(fun() -> enqueue(Store, N, [Sign*I || I <- lists:seq(K, Count, Concurrency)], Parent) end) || K <- lists:seq(1, Concurrency)],
    [receive enqueued -> ok after 120000 -> error(enqueue_timeout) end || _ <- lists:seq(1, Concurrency)],
    io:format("ENQUEUE concurrency=~p count=~p ms=~p jobs_s=~.2f~n", [Concurrency, Count, now_ms()-T, Count*1000/max(1,now_ms()-T)]).
worker(Store, Controller, Count, Offset) ->
    worker_loop(Store, Controller, Count, Offset, 0).
worker_loop(Store, C, Count, Offset, Step) ->
    I = (Offset + Step) rem Count + 1,
    try
        {ok, J} = Store:get(I),
        Now = now_ms(),
        Can = case maps:get(state, J) of queued -> true; {claimed, _, Until} -> Until < Now; done -> false end,
        case Can of
            true ->
                Claimed = J#{state => {claimed, node(), Now+2000}, attempts => maps:get(attempts,J)+1},
                case Store:change(I, J, Claimed) of
                    {ok, changed} ->
                        ok = rpc:call(C, ?MODULE, event, [I, {execution, maps:get(attempts,Claimed)}]),
                        timer:sleep(1 + I rem 5),
                        case Store:change(I, Claimed, Claimed#{state => done}) of
                            {ok, changed} -> rpc:call(C, ?MODULE, event, [I, done]);
                            _ -> ok
                        end;
                    _ -> ok
                end;
            false -> ok
        end
    catch _:_ -> timer:sleep(20) end,
    worker_loop(Store, C, Count, Offset, Step+1).
workers(Store, Nodes, Count) ->
    [spawn(N, ?MODULE, worker, [Store, node(), Count, K*313]) || {N,K} <- lists:zip(Nodes,lists:seq(0,length(Nodes)-1))].
done_count() -> length([I || {{I,done},_} <- ets:tab2list(events)]).
await_done(Store, Node, Count, Deadline) ->
    case done_count() > Count-10 of
        true -> [case rpc:call(Node,Store,get,[I],1000) of
                     {ok,#{state := done}} -> event(I,done);
                     _ -> ok
                 end || I <- lists:seq(1,Count),not ets:member(events,{I,done})];
        false -> ok
    end,
    case done_count() of
        Count -> ok;
        _ -> case now_ms() > Deadline of true -> error({work_timeout,done_count()}); false -> timer:sleep(100), await_done(Store,Node,Count,Deadline) end
    end.
await_progress(Target) -> case done_count() >= Target of true -> ok; false -> timer:sleep(10), await_progress(Target) end.
stop_workers(Pids) -> [exit(P,kill) || P <- Pids], timer:sleep(100).
snapshot(Store,N,Count) -> maps:from_list([{I,rpc:call(N,Store,get,[I],60000)} || I <- lists:seq(1,Count)]).
hold_claim(Store, Controller) ->
    {ok, Job} = Store:get(1),
    Until = now_ms()+2000,
    Claimed = Job#{state => {claimed,node(),Until}, attempts => 1},
    {ok,changed} = Store:change(1,Job,Claimed),
    Controller ! {held,node(),Until},
    timer:sleep(10000),
    Store:change(1,Claimed,Claimed#{state => done}).
lease(Store) ->
    logger:set_primary_config(level,error),
    ets:new(events,[named_table,public,set]),
    {ok,Host}=inet:gethostname(),
    Nodes=[list_to_atom("durable"++integer_to_list(I)++"@"++Host) || I <- lists:seq(1,3)],
    [begin
         ets:delete_all_objects(events),
         Root=filename:absname(atom_to_list(Store)++"-lease-"++atom_to_list(Role)++"-"++integer_to_list(now_ms())),
         ok=filelib:ensure_dir(Root++"/placeholder"),
         [launch(Store,N,Root) || N <- Nodes],
         ok=Store:setup(Nodes), [First|_]=Nodes,
         Leader=rpc:call(First,Store,leader,[]),
         Dead=case Role of leader -> Leader; follower -> hd(Nodes--[Leader]) end,
         [Live|_]=Nodes--[Dead],
         ok=rpc:call(First,Store,put,[1,#{id=>1,payload=><<>>,state=>queued,attempts=>0}]),
         Holder=spawn(Dead,?MODULE,hold_claim,[Store,self()]),
         Until=receive {held,Dead,U} -> U after 10000 -> error(claim_timeout) end,
         {ok,#{state := {claimed,Dead,Until},attempts := 1}}=rpc:call(Live,Store,get,[1]),
         true=rpc:call(Dead,erlang,is_process_alive,[Holder]),
         io:format("LEASE_HELD store=~p role=~p holder=~p job=1 attempts=1 lease_remaining_ms=~p~n",[Store,Role,Dead,Until-now_ms()]),
         KillTime=now_ms(), kill(Dead),
         Worker=spawn(Live,?MODULE,worker,[Store,node(),1,0]),
         await_done(Store,Live,1,now_ms()+15000),
         exit(Worker,kill),
         {ok,#{state := done,attempts := 2}}=rpc:call(Live,Store,get,[1]),
         [{{1,{execution,2}},ReclaimedAt}]=ets:lookup(events,{1,{execution,2}}),
         true=ReclaimedAt>Until,
         io:format("LEASE_RECOVERED store=~p role=~p worker=~p job=1 state=done attempts=2 reclaim_after_expiry_ms=~p reclaim_after_kill_ms=~p~n",[Store,Role,Live,ReclaimedAt-Until,ReclaimedAt-KillTime]),
         [kill(N) || N <- Nodes--[Dead]], timer:sleep(100)
     end || Role <- [follower,leader]], ok.
run(Store) -> run(Store,false).
run(Store, SyncLog) ->
    logger:set_primary_config(level,error),
    ets:new(events,[named_table,public,set]), ets:new(acks,[named_table,public,set]),
    {ok, Host} = inet:gethostname(),
    Nodes = [list_to_atom("durable"++integer_to_list(I)++"@"++Host) || I <- lists:seq(1,3)],
    Root = filename:absname(atom_to_list(Store)++"-"++integer_to_list(now_ms())),
    ok = filelib:ensure_dir(Root++"/placeholder"),
    io:format("ENV store=~p otp=~s schedulers=~p root=~s~n",[Store,erlang:system_info(otp_release),erlang:system_info(logical_processors_available),Root]),
    [launch(Store,N,Root) || N <- Nodes],
    ok = Store:setup(Nodes), [First|_] = Nodes,
    produce(Store,First,10000,1), produce(Store,First,10000,16),
    T = now_ms(), Ws = workers(Store,Nodes,10000), await_progress(1000),
    Dead = rpc:call(First,Store,leader,[]), Survivors = Nodes--[Dead],
    Before = done_count(), KillTime=now_ms(), kill(Dead),
    await_progress(Before+1),
    CompletionMs = now_ms()-KillTime,
    [Live|_] = Survivors,
    NewLeader = rpc:call(Live,Store,leader,[],60000),
    true = NewLeader =/= Dead,
    ok = rpc:call(Live,Store,put,[0,#{state => marker}],60000),
    io:format("FAILOVER first_completion_ms=~p leader_and_write_ms=~p new_leader=~p~n",[CompletionMs,now_ms()-KillTime,NewLeader]),
    ready(Store,Live,600), launch(Store,Dead,Root), ready(Store,Dead,600),
    io:format("REJOIN ~p read_ok=true~n",[Dead]),
    io:format("REJOIN_MEMBERSHIP ~p~n",[case Store of
        store_khepri -> rpc:call(Dead,ra,members,[{local,{jobs,Dead}}]);
        store_mnesia -> rpc:call(Dead,mnesia,system_info,[running_db_nodes])
    end]),
    await_done(Store,Live,10000,now_ms()+240000), stop_workers(Ws),
    Finish = now_ms(),
    Snap = snapshot(Store,Live,10000),
    Lost = length([I || {I,V} <- maps:to_list(Snap), not is_map(element(2,V))]),
    Attempts = lists:foldl(fun({{I,{execution,_}},_},Acc) -> maps:update_with(I,fun(X)->X+1 end,1,Acc); (_,Acc)->Acc end,#{},ets:tab2list(events)),
    Dups = length([I || {I,C} <- maps:to_list(Attempts),C>1]),
    Reclaims = length([I || {I,{ok,J}} <- maps:to_list(Snap),maps:get(attempts,J)>1]),
    io:format("SINGLE_CRASH ack=~p lost=~p duplicate_jobs=~p reclaimed_jobs=~p elapsed_ms=~p claim_complete_jobs_s=~.2f~n",[ets:info(acks,size),Lost,Dups,Reclaims,Finish-T,10000000/max(1,Finish-T)]),
    ets:delete_all_objects(events), produce(Store,Live,10000,16),
    Ws2=workers(Store,Nodes,10000), await_progress(1000),
    %% Stop application workers, not nodes: freeze a committed-state oracle mid-workload.
    stop_workers(Ws2),
    case SyncLog of
        true -> [begin ok=rpc:call(N,mnesia,sync_log,[]), io:format("SYNC_LOG node=~p result=ok~n",[N]) end || N <- Nodes];
        false -> ok
    end,
    io:format("FULL_CRASH_MODE sync_log=~p~n",[SyncLog]),
    BeforeCrash=snapshot(Store,Live,10000),
    [kill(N) || N <- Nodes], [launch(Store,N,Root) || N <- Nodes],
    [ready(Store,N,600) || N <- Nodes],
    AfterCrash=snapshot(Store,First,10000),
    [io:format("RECOVERED_REPLICA ~p mismatches_vs_first=~p~n",[N,length([I || {I,V} <- maps:to_list(snapshot(Store,N,10000)),maps:get(I,AfterCrash)=/=V])]) || N <- tl(Nodes)],
    Differences=[{I,V,maps:get(I,AfterCrash)} || {I,V} <- maps:to_list(BeforeCrash),maps:get(I,AfterCrash)=/=V],
    Missing=length([I || {I,V} <- maps:to_list(AfterCrash),not (is_tuple(V) andalso element(1,V)=:=ok)]),
    ok=file:write_file(Root++"/snapshots.term",term_to_binary({BeforeCrash,AfterCrash})),
    io:format("FULL_CRASH acknowledged=~p missing=~p exact_state_mismatches=~p examples=~p~n",[ets:info(acks,size),Missing,length(Differences),lists:sublist(Differences,3)]),
    io:format("DISK ~s",[os:cmd("du -sk "++Root)]),
    io:format("VERSIONS ~p~n",[[{A,rpc:call(First,application,get_key,[A,vsn])} || A <- [khepri,ra,mnesia]]]),
    [kill(N) || N <- Nodes], ok.
