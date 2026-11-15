-module(room).
-behaviour(gen_server).
-export([init/1, handle_call/3, handle_cast/2]).

init(Entity) -> {ok, {Entity, 0}}.

handle_call({increment, Id, Entity, 1}, _From, {Entity, Count}) ->
    NewCount = Count + 1,
    {reply, {applied, Id, Entity}, {Entity, NewCount}};
handle_call(snapshot, _From, State = {_Entity, Count}) ->
    {reply, Count, State};
handle_call(reset, _From, {Entity, _Count}) ->
    {reply, ok, {Entity, 0}};
handle_call(crash, _From, _State) -> error(injected_handler_failure).

handle_cast(Request, _State) -> error({unexpected_cast, Request}).
