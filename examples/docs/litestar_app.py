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
