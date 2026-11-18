# Litestar

Use Litestar's store API to share values across requests. `BeamStore` keeps each store's keys in its own namespace.

```sh
uv add 'python-beam[litestar]' uvicorn
```

## Channels

Add `ChannelsPlugin` with `BeamChannelsBackend` to send notifications to Litestar subscribers. This example uses both PyBEAM adapters: publishing changes the stored status, then sends `changed` to connected event-stream clients. Clients read `/status` for the current value rather than treating a notification as stored state.

```python
# litestar_app.py
import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import override

from litestar import Litestar, Request, get, post
from litestar.channels import ChannelsPlugin
from litestar.datastructures import State
from litestar.response import ServerSentEventMessage
from litestar.response.streaming import ASGIStreamingResponse
from litestar.types import Receive, Scope, Send
from uvicorn import Config, Server

from pybeam.integrations.hornbeam import Beam, run
from pybeam.integrations.litestar.channels import BeamChannelsBackend
from pybeam.integrations.litestar.store import BeamStore

backend = BeamChannelsBackend()
channels = ChannelsPlugin(
    backend,
    channels=["documents"],
    subscriber_max_backlog=16,
)


@asynccontextmanager
async def lifespan(app: Litestar) -> AsyncGenerator[None]:
    beam = await Beam.connect()
    app.stores.register("documents", BeamStore(beam, "litestar:documents"))
    await app.stores.get("documents").set("status", "draft")
    yield


@get("/status", media_type="text/plain")
async def status(request: Request[object, object, State]) -> str:
    value = await request.app.stores.get("documents").get("status")
    return value.decode("utf-8") if value is not None else ""


@post("/publish", status_code=200, media_type="text/plain")
async def publish(request: Request[object, object, State]) -> str:
    await request.app.stores.get("documents").set("status", "published")
    await channels.wait_published(b"changed", "documents")
    return "published"


class DocumentEvents(ASGIStreamingResponse):
    def __init__(self) -> None:
        super().__init__(iterator=(), media_type="text/event-stream")

    async def data(self, src: AsyncGenerator[bytes]) -> AsyncGenerator[bytes]:
        yield b"data: subscribed\n\n"
        async for payload in src:
            yield ServerSentEventMessage(data=payload.decode("utf-8")).encode()

    @override
    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        async with backend.subscription(channels, "documents") as updates:
            messages = self.data(updates)
            self.iterator = messages
            try:
                await super().__call__(scope, receive, send)
            finally:
                await messages.aclose()


@get("/events")
async def events() -> DocumentEvents:
    return DocumentEvents()


app = Litestar(
    route_handlers=[status, publish, events],
    lifespan=[lifespan],
    plugins=[channels],
)


async def main() -> None:
    config = Config(app, host="127.0.0.1", port=8768, loop="none", http="h11")
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
$ uv run litestar_app.py
```

The server listens on port 8768. In another terminal, read and update the document status:

```console
$ curl http://127.0.0.1:8768/status
draft
$ curl -X POST http://127.0.0.1:8768/publish
published
$ curl http://127.0.0.1:8768/status
published
```

The store accepts strings or bytes and returns bytes. Values live in memory and are lost when the program stops; this example also resets the status during startup. Stop the server with Ctrl+C.

Open the event stream in another terminal and wait for `subscribed` before publishing:

```console
$ curl -N http://127.0.0.1:8768/events
data: subscribed

```

Each publish adds a `data: changed` event. Stop curl with Ctrl+C to disconnect.

Notifications are not durable, and this backend provides no history. `wait_published()` does not acknowledge delivery to subscribers. The helper propagates backend receive failures and releases subscriptions on exit. The backend merge queue holds up to 128 messages; overflow ends the backend stream. Each subscriber in this example buffers up to 16 messages; Litestar drops new messages when that backlog is full. Treat notifications as invalidations, not a reliable event log.

The [full Litestar example](../../../examples/litestar/main.py) adds templates, an atomic counter and live updates.
