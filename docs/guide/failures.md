# Startup, reconnects, and failures

Treat a restart as a new execution and a reconnect as a new connection or request.
PyBEAM can restart work, but it does not make every value or message durable.
Choose what to save, and rebuild each client's view from authorized current state.

## Initialize before serving requests

In an ASGI application's lifespan startup, you can connect to shared services,
initialize state, and create and call actors before yielding to serve requests.
Keep resources inside their owning lifespan or actor group.

If lifespan startup raises an exception, startup stops and the original error
appears in the startup error output. Requests are not served by that failed
startup. Fix the error before retrying; do not treat a process starting as proof
that your application is ready.

See [Shared state](state.md), [Actors](actors.md), and your
[FastAPI](integrations/fastapi.md) or [Litestar](integrations/litestar.md)
integration for resource setup.

## Rebuild a view after reconnect

A WebSocket reconnect, a PyView live view reconnect, and a new SSE stream request
run application code again. They do not migrate the previous connection or replay
missed pub/sub messages. Authenticate and authorize each new connection or request,
even if the previous one was accepted. Subscribe to the authorized data, then read
current state to render a fresh view. Re-read state on each notification.

Do not send a view before authorization succeeds. Rejection depends on the
transport and your application's authorization policy:

- **WebSocket:** the handler below closes before accepting. With Uvicorn, as used
  by this example, the client receives HTTP `403` and no WebSocket connection.
  With PyBEAM's native web server, the same close-before-accept pattern completes
  the handshake and then delivers close code `1008`.
- **PyView:** session authorization using PyView's required-scope check runs again
  before the connected mount. A rejected join closes with code `1000` and sends
  no mounted view. An accepted reconnect mounts a new view from current state.
- **SSE:** reject unauthorized requests before opening the event stream. A handler
  that raises HTTP `403` returns that status, not a stream or a current-state event.
  An accepted request should send current state as its first event.

This example uses a fixed demo token for a shared counter. Replace the token check
with your application's authentication and data authorization before using it for
private data. The increment endpoint is public in this example.

```python
# reconnect.py
import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from uvicorn import Config, Server

from pybeam.integrations.fastapi.dependencies import Beam, provider
from pybeam.integrations.hornbeam import run

app = FastAPI(lifespan=provider.lifespan)


@app.post("/increment")
async def increment(beam: Beam) -> dict[str, int]:
    count = await beam.incr("visits", namespace="reconnect")
    await beam.publish("reconnect:changed", b"invalidate")
    return {"count": count}


async def send_updates(socket: WebSocket, beam: Beam) -> None:
    async with beam.subscribe("reconnect:changed", max_bytes=4096) as changes:
        count = await beam.incr("visits", 0, namespace="reconnect")
        await socket.send_json({"count": count})
        async for _message in changes:
            count = await beam.incr("visits", 0, namespace="reconnect")
            await socket.send_json({"count": count})


@app.websocket("/counter")
async def counter(socket: WebSocket, beam: Beam) -> None:
    if socket.query_params.get("token") != "demo-token":
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        async with asyncio.TaskGroup() as tasks:
            sending = tasks.create_task(send_updates(socket, beam))
            try:
                async for _message in socket.iter_text():
                    pass
            finally:
                sending.cancel()
    except* WebSocketDisconnect:
        pass


async def main() -> None:
    config = Config(app, host="127.0.0.1", port=8770, loop="none", http="h11")
    server = Server(config)
    try:
        await server.serve()
    except asyncio.CancelledError:
        if server.started:
            await server.shutdown()
        raise


if __name__ == "__main__":
    run(main)
```

```console
$ uv run reconnect.py
```

The server listens on port 8770. Connect a WebSocket client to
`ws://127.0.0.1:8770/counter?token=demo-token`, then send HTTP POST requests to
`http://127.0.0.1:8770/increment` to receive updated counts. Disconnect and reconnect
to receive the current count. Stop the server with Ctrl+C.

Subscribing before the initial read avoids a gap between reading state and
starting to receive notifications. A notification may refer to a change already
included in your initial read; reading current state again is safe.

Messages published while the client has no subscription are missed. Reconnecting
does not recover them. If your application needs a history, store it separately
and authorize reads of that history. An SSE `Last-Event-ID` header does not make
pub/sub durable: a snapshot-and-subscribe handler without a history lookup sends
current state, not events since that ID. Implement history lookup explicitly if
you need replay. See [Messaging](messaging.md) and the
[PyView integration](integrations/pyview.md).

## Allow time for SSE disconnect cleanup

An SSE subscription closes when PyBEAM detects that its client is gone, not
necessarily when the browser loses its network connection. With PyBEAM's native
web server on Linux, real-client checks measured these subscription cleanup times:

| Client loss | Idle stream | Stream receiving updates |
| --- | --- | --- |
| Client process killed | Up to 15.2 seconds | Under 0.02 seconds |
| Network cut without a clean disconnect | 35.7–45.1 seconds | 20.4–20.7 seconds |

Both request-owned subscriptions and subscriptions opened through lifespan
services were released, and their stream generators finished. These are observed
times, not a deadline for every deployment. FastAPI's SSE responses already send
keepalive comments while idle; this pattern needs no new PyBEAM setting. A custom
stream that never writes cannot detect silent network loss this way.

If a proxy keeps its connection to PyBEAM open after the client disappears,
cleanup can wait until the proxy's own timeout. Choose proxy timeouts that fit
your application's cleanup needs.

## Know what survives a failure

| Failure or boundary | Lost or stopped | What survives or recovers |
| --- | --- | --- |
| Actor crash followed by a supervised restart | The actor's unsaved in-memory changes. An ordinary actor starts again from its constructor state. Calls already running or queued for the crashed actor fail with `RuntimeError`; they are not carried over to the replacement actor. | Shared counters and store values on the still-running node. An actor restored from explicitly saved state recovers its saved values, not later unsaved changes. |
| Runtime stop or node loss | That node's in-memory shared counters and store values, and its live connections and subscriptions. | Explicitly saved actor state survives a whole-runtime process kill when its saved files remain available. Durable task records can recover as described below. |
| WebSocket disconnect | The connection and its subscription; messages published during the gap are not replayed. | Shared state on the running node. A new authorized connection can read it to build a fresh view. |
| Client falls behind its subscription's unread-message limit | The subscription fails and is released instead of retaining an unbounded backlog. A nonreading WebSocket can also be disconnected. | Shared state remains available for a fresh read after reconnecting. |
| Orderly shutdown with plain background tasks still running | The tasks are cancelled; cooperative cleanup can run within the shutdown budget. | Cleanup effects that completed. Cancellation-resistant work cannot report a clean shutdown. |
| Durable task worker-node loss | The current execution does not continue from its suspended position. | A serving replica can rerun the recorded task from the beginning. Acknowledged submissions also recover after a runtime process crash when their stored data remains available. |

Do not treat ordinary background tasks as recorded jobs. The shutdown behavior
above is cancellation, not durable recovery. An abrupt process kill cannot run
cooperative cleanup.

Saved actor state and durable tasks depend on keeping their data files. Neither
supervision nor shared in-memory state replaces persistent storage. Recovery from
a process crash does not establish recovery from disk loss or power failure.

Durable tasks can repeat external effects: design task bodies for retries. Read
the [durable task storage and replication limits](durable-tasks.md) before relying
on recovery. See [Supervision](supervision.md) for replacement actor references
and [Actors](actors.md) for call timeout behavior.
