-module(py_supervision_demo).
-behaviour(gen_server).

-export([start_link/1, register_callbacks/0, init/1, handle_call/3,
         handle_cast/2, handle_info/2, terminate/2]).

%% Internal OTP regression worker, not a Python actor runtime.
start_link(Events) -> gen_server:start_link(?MODULE, Events, []).

register_callbacks() ->
    py_callback:register('_supervision_demo_read', fun([Pid]) when is_pid(Pid) ->
        gen_server:call(Pid, read, 2000)
    end),
    py_callback:register('_supervision_demo_increment', fun([Pid]) when is_pid(Pid) ->
        gen_server:call(Pid, increment, 2000)
    end),
    py_callback:register('_supervision_demo_crash', fun([Pid]) when is_pid(Pid) ->
        Ref = monitor(process, Pid),
        gen_server:cast(Pid, crash),
        receive
            {'DOWN', Ref, process, Pid, deliberate_crash} -> true
        after 2000 ->
            demonitor(Ref, [flush]),
            erlang:error(crash_not_confirmed)
        end
    end),
    ok.

init(Events) ->
    process_flag(trap_exit, true),
    ok = py_channel:send(Events, {<<"started">>, self()}),
    {ok, {Events, 0}}.

handle_call(read, _From, {_, Value} = State) -> {reply, Value, State};
handle_call(increment, _From, {Events, Value}) ->
    {reply, Value + 1, {Events, Value + 1}}.

handle_cast(crash, State) -> {stop, deliberate_crash, State}.
handle_info(_Message, State) -> {noreply, State}.

terminate(Reason, {Events, _Value}) ->
    ok = py_channel:send(Events, {<<"stopped">>, self(), atom_to_binary(Reason)}).
