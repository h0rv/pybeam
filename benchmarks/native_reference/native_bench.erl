-module(native_bench).
-export([main/0]).

main() ->
    [Directory, Mode] = init:get_plain_arguments(),
    try
        {ok, ConfigBytes} = file:read_file(filename:join(Directory, "config.json")),
        Config = json:decode(ConfigBytes),
        Workload = maps:get(<<"workload">>, Config),
        ManifestPath = filename:join(filename:dirname(code:which(?MODULE)), "manifest.json"),
        {ok, ManifestBytes} = file:read_file(ManifestPath),
        Manifest = json:decode(ManifestBytes),
        Assets = maps:get(<<"assets">>, Manifest),
        lists:foreach(fun(#{<<"path">> := Path, <<"sha256">> := Digest}) ->
            Digest = hash_file(Path)
        end, Assets ++ maps:get(<<"sources">>, Manifest)),
        lists:foreach(fun(Module) ->
            {module, Module} = code:ensure_loaded(Module),
            Loaded = unicode:characters_to_binary(code:which(Module)),
            true = lists:any(fun(#{<<"path">> := Path}) -> Path =:= Loaded end, Assets)
        end, [room, native_bench]),
        OTPPath = filename:join([code:root_dir(), "releases", erlang:system_info(otp_release), "OTP_VERSION"]),
        {ok, OTPBytes} = file:read_file(OTPPath),
        OTP = string:trim(OTPBytes),
        <<"29.1.2">> = OTP,
        Executable = filename:join([code:root_dir(), "erts-" ++ erlang:system_info(version), "bin", "beam.smp"]),
        Runtime = #{python => null, otp => OTP,
            erts => list_to_binary(erlang:system_info(version)), elixir => null,
            mode => <<"native_otp_gen_server_local_single_vm">>,
            gil_enabled => null, interpreters => null,
            workers => maps:get(<<"entities">>, Workload),
            schedulers => erlang:system_info(schedulers_online),
            executable => unicode:characters_to_binary(Executable),
            executable_sha256 => hash_file(Executable), native_source_sha => null,
            loaded_assets => Assets ++ maps:get(<<"sources">>, Manifest)},
        ok = file:write_file(filename:join(Directory, "build-manifest.json"), ManifestBytes),
        Result = run(Directory, Workload, Runtime, Mode),
        write_json(Directory, "native-result.json", Result),
        Receipts = maps:get(receipts, Result),
        ok = file:write_file(filename:join(Directory, "native-receipts.jsonl"),
            [[json:encode(Receipt), $\n] || Receipt <- Receipts]),
        %% Publish actual OTP executable, adapter BEAM and source hash evidence.
        %% No paired PyBEAM NIF revision is invented for this pure OTP runtime.
        write_json(Directory, "adapter.json", Result),
        halt(0)
    catch Class:Reason:Stack ->
        write_json(Directory, "failure.json", #{class => atom_to_binary(Class),
            reason => unicode:characters_to_binary(io_lib:format("~tp", [Reason]))}),
        io:format(standard_error, "~tp:~tp ~tp~n", [Class, Reason, Stack]),
        halt(1)
    end.

run(Directory, W, Runtime, Mode) ->
    N = maps:get(<<"entities">>, W),
    Seed = maps:get(<<"seed">>, W),
    Warmup = sequence(maps:get(<<"warmup_events">>, W), Seed, N),
    Measured = sequence(maps:get(<<"events">>, W), Seed, N),
    Limit = maps:get(<<"max_inflight">>, W),
    Origin = erlang:monotonic_time(nanosecond),
    CreationStart = now_ns(Origin),
    Workers = [begin {ok, Pair} = gen_server:start_monitor(room, Entity, []), Pair end
        || Entity <- lists:seq(0, N - 1)],
    Creation = now_ns(Origin) - CreationStart,
    Pids = list_to_tuple([Pid || {Pid, _Ref} <- Workers]),
    try
        case Mode of
            "failure" ->
                Req = gen_server:send_request(element(1, Pids), crash),
                {error, _} = gen_server:receive_response(Req, 1000),
                error(expected_failure_probe);
            "run" -> ok
        end,
        WarmStart = now_ns(Origin),
        {_WarmReceipts, _WarmPeak} = phase(Warmup, Pids, Limit, Origin),
        WarmCounts = [gen_server:call(Pid, snapshot, 1000) || {Pid, _} <- Workers],
        [ok = gen_server:call(Pid, reset, 1000) || {Pid, _} <- Workers],
        ResetCounts = [gen_server:call(Pid, snapshot, 1000) || {Pid, _} <- Workers],
        true = lists:all(fun(Count) -> Count =:= 0 end, ResetCounts),
        WarmDuration = now_ns(Origin) - WarmStart,
        Start = now_ns(Origin),
        {RawReceipts, Peak} = phase(Measured, Pids, Limit, Origin),
        Duration = now_ns(Origin) - Start,
        Counts = [gen_server:call(Pid, snapshot, 1000) || {Pid, _} <- Workers],
        {Deletion, Deleted} = cleanup(Directory, Workers, Origin),
        write_json(Directory, "reset-proof.json", #{warmup_counts => WarmCounts,
            reset_counts => ResetCounts, same_workers => true,
            worker_pids => [list_to_binary(pid_to_list(Pid)) || {Pid, _} <- Workers]}),
        #{schema_version => 1, kind => result, backend => erlang, workload => W,
            runtime => Runtime, clock => sender_monotonic_ns,
            phases => #{creation_ns => Creation, warmup_ns => WarmDuration,
                measurement_ns => Duration, deletion_ns => Deletion},
            receipts => [#{id => Id, entity => Entity, sent_ns => Sent,
                completed_ns => Completed} || {Id, Entity, Sent, Completed} <- RawReceipts],
            transport_encoding => <<"BEAM local integer term copy; no ETF serialization; envelopes excluded">>,
            transport_payload_bytes => null, terminal_counts => Counts,
            entities_created => N, entities_deleted => Deleted, queue_peak => null,
            inflight_peak => Peak, failures => 0, recovered => null, lost => 0,
            cleanup_acknowledged => true,
            semantic_differences => [<<"Client admission only: unbounded destination gen_server mailboxes; no finite mailbox claim">>,
                <<"No PyBeam NIF or paired native checkout is loaded; OTP crypto hashes build assets outside timing">>],
            unsupported_features => [<<"destination_queue">>]}
    catch Class:Reason:Stack ->
        cleanup(Directory, Workers, Origin),
        erlang:raise(Class, Reason, Stack)
    end.

sequence(Count, Seed, N) -> [{Id, (Id + Seed) rem N} || Id <- lists:seq(0, Count - 1)].
now_ns(Origin) -> erlang:monotonic_time(nanosecond) - Origin.

phase(Messages, Pids, Limit, Origin) ->
    submit(Messages, Pids, Limit, Origin, gen_server:reqids_new(), [], 0).

submit([], _Pids, _Limit, Origin, Requests, Receipts, Peak) ->
    {Done, _Empty} = drain(Requests, Receipts, Origin),
    {lists:reverse(Done), Peak};
submit([{Id, Entity} | Rest], Pids, Limit, Origin, Requests, Receipts, Peak) ->
    %% Timestamp before waiting for a slot: admission waiting is included.
    Sent = now_ns(Origin),
    {Available, Done} = case gen_server:reqids_size(Requests) >= Limit of
        true -> receive_one(Requests, Receipts, Origin);
        false -> {Requests, Receipts}
    end,
    Next = gen_server:send_request(element(Entity + 1, Pids),
        {increment, Id, Entity, 1}, {Id, Entity, Sent}, Available),
    submit(Rest, Pids, Limit, Origin, Next, Done,
        max(Peak, gen_server:reqids_size(Next))).

receive_one(Requests, Receipts, Origin) ->
    case gen_server:receive_response(Requests, 1000, true) of
        {{reply, {applied, Id, Entity}}, {Id, Entity, Sent}, Next} ->
            Completed = now_ns(Origin),
            {Next, [{Id, Entity, Sent, Completed} | Receipts]};
        Other -> error({invalid_completion, Other})
    end.

drain(Requests, Receipts, Origin) ->
    case gen_server:reqids_size(Requests) of
        0 -> {Receipts, Requests};
        _ -> {Next, Done} = receive_one(Requests, Receipts, Origin),
            drain(Next, Done, Origin)
    end.

cleanup(Directory, Workers, Origin) ->
    Start = now_ns(Origin),
    [case is_process_alive(Pid) of
        true -> exit(Pid, shutdown);
        false -> ok
    end || {Pid, _} <- Workers],
    Reasons = [receive
        {'DOWN', Ref, process, Pid, Reason} ->
            #{pid => list_to_binary(pid_to_list(Pid)),
                reason => unicode:characters_to_binary(io_lib:format("~tp", [Reason]))}
        after 1000 -> error({deletion_timeout, Pid})
    end || {Pid, Ref} <- Workers],
    Duration = now_ns(Origin) - Start,
    write_json(Directory, "deletion-proof.json", #{downs => Reasons,
        deleted => length(Reasons), all_dead => lists:all(
            fun({Pid, _}) -> not is_process_alive(Pid) end, Workers)}),
    {Duration, length(Reasons)}.

hash_file(Path) ->
    {ok, Bytes} = file:read_file(Path),
    binary:encode_hex(crypto:hash(sha256, Bytes), lowercase).

write_json(Directory, Name, Value) ->
    ok = file:write_file(filename:join(Directory, Name), [json:encode(Value), $\n]).
