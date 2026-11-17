from dataclasses import dataclass
from typing import Literal

from pybeam.codec import Json


@dataclass(frozen=True, slots=True)
class Stat:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class Resource:
    kind: Literal["actor", "tasks", "queue"]
    identity: str
    name: str
    state: str
    stats: tuple[Stat, ...]


@dataclass(frozen=True, slots=True)
class Report:
    resources: tuple[Resource, ...]
    unavailable: tuple[str, ...]


REPORT = Json(Report)

# Read the pinned runtime's admission gates and queue metadata, never user payloads.
NATIVE_INSPECT = """
Inspect = fun() ->
    Text = fun(T) -> unicode:characters_to_binary(io_lib:format("~p", [T])) end,
    Stat = fun(K, V) -> #{name => K, value => Text(V)} end,
    GateStats = fun(G, Occupied) ->
        [Stat(<<"Admitted calls">>, maps:get(count, G)),
         Stat(<<"Free slots">>, max(0, maps:get(capacity, G) - Occupied)),
         Stat(<<"Capacity">>, maps:get(capacity, G)),
         Stat(<<"Encoded bytes">>, maps:get(bytes, G)),
         Stat(<<"Byte limit">>, maps:get(max_bytes, G)),
         Stat(<<"Unsettled calls">>, maps:get(unsettled, G))]
    end,
    GateState = fun(G, Occupied) ->
        case maps:get(closed, G) of
            true -> <<"draining">>;
            false -> case Occupied >= maps:get(capacity, G) of
                true -> <<"full">>; false -> <<"running">> end
        end
    end,
    Actors = fun() ->
        [begin
            G = py_nif:actor_gate_info(Gate),
            Occupied = maps:get(count, G),
            Mailbox = case process_info(Pid, message_queue_len) of
                {message_queue_len, N} -> N; undefined -> 0 end,
            #{kind => <<"actor">>, identity => Text({Group, Id}), name => Id,
              state => case is_process_alive(Pid) of
                  true -> GateState(G, Occupied); false -> <<"restarting">> end,
              stats => [Stat(<<"Generation">>, Generation),
                        Stat(<<"Mailbox messages">>, Mailbox) | GateStats(G, Occupied)]}
        end || {{Group, Id}, Pid, Generation, Gate, _, _}
                   <- ets:tab2list(py_actor_generations)]
    end,
    Tasks = fun() ->
        [begin
            #{gate := Gate, jobs := Jobs} = sys:get_state(Pid, 100),
            G = py_nif:actor_gate_info(Gate),
            Occupied = max(map_size(Jobs), maps:get(count, G)),
            #{kind => <<"tasks">>, identity => Text(Pid), name => <<"Task supervisor">>,
              state => GateState(G, Occupied),
              stats => [Stat(<<"Owned results">>, map_size(Jobs)) | GateStats(G, Occupied)]}
        end || {_, Pid, _, [py_task_group]} <- supervisor:which_children(py_actor_root),
               is_pid(Pid)]
    end,
    Queues = fun() ->
        case whereis(py_durable_queue) of
            undefined -> [];
            _ ->
                #{queues := Open} = sys:get_state(py_durable_queue, 100),
                Now = erlang:system_time(millisecond),
                [begin
                    [{dq_meta, _, {Max, Count}}] = mnesia:dirty_read(dq_meta, {queue, Q}),
                    Leased = ets:select_count(dq_item,
                        [{{dq_item, '_', Q, '_', '_', '_', {leased, '_', '$1'}, '_'},
                          [{'>', '$1', Now}], [true]}]),
                    #{kind => <<"queue">>, identity => Q, name => Q,
                      state => case Count >= Max of true -> <<"full">>;
                          false -> <<"open">> end,
                      stats => [Stat(<<"Queue depth">>, max(0, Count - Leased)),
                                Stat(<<"Active leases">>, Leased),
                                Stat(<<"Capacity">>, Max)]}
                end || Q <- maps:keys(Open)]
        end
    end,
    {Rows, Errors} = lists:foldl(fun({Name, Read}, {Rs, Es}) ->
        try {Rs ++ Read(), Es}
        catch _:_ -> {Rs, Es ++ [Name]} end
    end, {[], []}, [{<<"Actors">>, Actors}, {<<"Tasks">>, Tasks}, {<<"Queues">>, Queues}]),
    iolist_to_binary(json:encode(#{resources => Rows, unavailable => Errors}))
end,
"""
