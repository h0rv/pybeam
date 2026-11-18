#!/usr/bin/env escript
%%! +S 2:2
main([Build, Bind, Root, Site]) ->
    main([Build, Bind, Root, Site, "benchmarks.web.app:app"]);
main([Build, Bind, Root, Site, App]) ->
    logger:set_primary_config(level, warning),
    Paths = filelib:wildcard(filename:join([Build, "checkouts/*/ebin"])) ++
            filelib:wildcard(filename:join([Build, "lib/*/ebin"])),
    ok = code:add_paths(Paths),
    application:load(hornbeam),
    application:set_env(erlang_python, num_contexts, 2),
    application:set_env(erlang_python, event_loop_pool_size, 1),
    application:set_env(hornbeam, context_pool_size, 2),
    {ok, _} = application:ensure_all_started(hornbeam),
    Options = #{worker_class => asgi, context_mode => worker, num_contexts => 2,
                num_acceptors => 2, bind => Bind, lifespan => on,
                timeout => 30000, websocket_timeout => 3600000,
                websocket_protocol => asgi,
                pythonpath => [Root, filename:join(Root, "src"), Site]},
    ok = hornbeam:start(App, Options),
    io:format("WEB_READY~n"),
    timer:sleep(infinity).
