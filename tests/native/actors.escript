#!/usr/bin/env escript
%%! +S 2:2
-mode(compile).

main([Root, Runtime]) ->
    true = code:add_patha(filename:join(Runtime, "ebin")),
    io:format("Internal native actor regression fixture.~n"),
    io:format("Mailbox policy: explicit unbounded. No mailbox bounds.~n"),
    {ok, _} = application:ensure_all_started(erlang_python),
    {ok, Ctx} = py_context:new(#{mode => worker}),
    Mon = erlang:monitor(process, Ctx),
    try run(Ctx, Root)
    after
        ok = py_context:stop(Ctx),
        receive {'DOWN', Mon, process, Ctx, normal} -> ok end,
        ok = application:stop(erlang_python)
    end,
    false = is_process_alive(Ctx),
    false = lists:keymember(erlang_python, 1, application:which_applications()),
    io:format("Cleanup: both actors stopped, state collected, context stopped.~n").

run(Ctx, Root) ->
    {ok, Env} = py_context:create_local_env(Ctx),
    {ok, _} = py_context:eval(Ctx, <<"__import__('sys').path.insert(0, root)">>,
                            #{<<"root">> => unicode:characters_to_binary(Root)}, infinity, Env),
    ok = py_context:exec(Ctx, <<"from tests.native.actor_objects import Counter, show_results, verify_collected">>, Env),
    Factory = ref(Ctx, Env, <<"Counter">>, #{}),
    Add = ref(Ctx, Env, <<"Counter.add">>, #{}),
    Read = ref(Ctx, Env, <<"Counter.read">>, #{}),
    {ok, First} = py_object_actor:start_link(Ctx, Factory, [Add, Read], unbounded),
    {FirstWeak, SecondWeak} = try
        {ok, Second} = py_object_actor:start_link(Ctx, Factory, [Add, Read], unbounded),
        try demonstrate(Ctx, Env, First, Second, Add, Read)
        after stop(Second) end
    after stop(First) end,
    true = erlang:garbage_collect(Ctx),
    ok = py_context:exec(Ctx, <<"import gc; gc.collect()">>, Env),
    ref(Ctx, Env, <<"verify_collected(first, second)">>,
        #{<<"first">> => FirstWeak, <<"second">> => SecondWeak}),
    ok.

ref(Ctx, Env, Code, Bindings) ->
    {ok, Ref} = py_context:eval_ref(Ctx, Code, Bindings, Env),
    Ref.

stop(Pid) ->
    Mon = erlang:monitor(process, Pid),
    ok = py_object_actor:stop(Pid),
    receive {'DOWN', Mon, process, Pid, normal} -> ok end,
    false = is_process_alive(Pid).

demonstrate(Ctx, Env, First, Second, Add, Read) ->
    FirstWeak = weak(Ctx, Env, First),
    SecondWeak = weak(Ctx, Env, Second),
    One = ref(Ctx, Env, <<"(1,)">>, #{}),
    Visits = ref(Ctx, Env, <<"{'label': 'visits'}">>, #{}),
    Results = concurrent(First, Add, One, Visits),
    Empty = ref(Ctx, Env, <<"()">>, #{}),
    Tuple = lists:foldl(fun(Result, Acc) ->
        ref(Ctx, Env, <<"(head,) + tail">>, #{<<"head">> => Result, <<"tail">> => Acc})
    end, Empty, Results),
    Two = ref(Ctx, Env, <<"(2,)">>, #{}),
    Other = ref(Ctx, Env, <<"{'label': 'other'}">>, #{}),
    {ok, _} = py_object_actor:call(Second, Add, Two, Other, 5000),
    Kwargs = ref(Ctx, Env, <<"{}">>, #{}),
    {ok, FirstState} = py_object_actor:call(First, Read, Empty, Kwargs, 5000),
    {ok, SecondState} = py_object_actor:call(Second, Read, Empty, Kwargs, 5000),
    ref(Ctx, Env, <<"show_results(results, first, second)">>,
        #{<<"results">> => Tuple, <<"first">> => FirstState, <<"second">> => SecondState}),
    %% Only weak references leave this function. The runner checks collection
    %% after both gen_servers have stopped and their native requests are gone.
    {FirstWeak, SecondWeak}.

concurrent(Actor, Method, Args, Kwargs) ->
    Parent = self(),
    Tag = make_ref(),
    Clients = [spawn_monitor(fun() ->
        Parent ! {Tag, self(), py_object_actor:call(Actor, Method, Args, Kwargs, 5000)}
    end) || _ <- lists:seq(1, 8)],
    try [begin
        Result = receive {Tag, Pid, {ok, R}} -> R after 10000 -> error(call_timeout) end,
        receive {'DOWN', Mon, process, Pid, normal} -> ok after 10000 -> error(caller_timeout) end,
        Result
    end || {Pid, Mon} <- Clients]
    after lists:foreach(fun({Pid, Mon}) -> exit(Pid, kill), erlang:demonitor(Mon, [flush]) end, Clients)
    end.

weak(Ctx, Env, Actor) ->
    Parent = self(),
    {Pid, Mon} = spawn_monitor(fun() ->
        #{object := Object} = sys:get_state(Actor),
        Weak = ref(Ctx, Env, <<"__import__('weakref').ref(state)">>, #{<<"state">> => Object}),
        Parent ! {weak, self(), Weak}
    end),
    Weak = receive {weak, Pid, W} -> W after 5000 -> error(inspection_timeout) end,
    receive {'DOWN', Mon, process, Pid, normal} -> ok after 5000 -> error(inspection_timeout) end,
    Weak.
