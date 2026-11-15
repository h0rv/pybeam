# Web examples

These are source-checkout examples, not installed package commands. Prepare the runtime and optional dependencies with the [source workflow](quickstart.md#work-from-source) first. Launch commands do not implicitly build missing assets.

## Shared counters

From the repository root, run one framework example.

```sh
mise run example:fastapi
# Or choose example:litestar or example:pyview.
```

Open `http://127.0.0.1:8642/` in two tabs. Increment in either tab. Both read the same counter in BEAM ETS, where updates are atomic. Notifications trigger fresh reads. FastAPI and Litestar use SSE; PyView uses LiveView WebSockets. Restarting BEAM clears the counter.

These examples use Hornbeam's ASGI serving path and existing services. They do not prove a performance advantage over a Python server.

## Room miniapp

```sh
mise run example:discord --port 8766
```

Open `http://127.0.0.1:8766/rooms/general` in two separate browser sessions. Join before sending. The first session owns the crash button. After a crash, join the new room generation explicitly. State is empty after restart.

This Discord-style demo has no Discord authentication. Uvicorn serves FastAPI inside `ErlangEventLoop`. The Hornbeam integration owns its existing supervised services, but does not start Hornbeam's HTTP listener here.

Rooms have bounded history and subscription queues. Slow output disconnects after five seconds. Disconnect releases the subscription, not membership. Read the [miniapp README](../../examples/discord_demo/README.md) for exact limits and cleanup semantics.

## Showcase

`mise run showcase` opens the local experiment control plane on port 38921. Native measurement profiles require separately prepared trusted assets. This is a development tool, not a production service or an installation check. Do not treat its counters as comparative benchmark results.
