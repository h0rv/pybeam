from contextlib import asynccontextmanager
from logging import INFO, basicConfig, getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, override

from litestar import Litestar, Request, get, post
from litestar.datastructures import State
from litestar.enums import RequestEncodingType
from litestar.exceptions import HTTPException
from litestar.params import Body, FromPath
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.response import File, Response, ServerSentEventMessage, Template
from litestar.response.streaming import ASGIStreamingResponse
from litestar.template.config import TemplateConfig
from pydantic import BaseModel, Field

from pybeam.integrations.hornbeam import (
    Services,
    Subscription,
)
from pybeam.integrations.hornbeam.client import Beam
from pybeam.integrations.litestar.store import BeamStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from litestar.types import Receive, Scope, Send

TOPIC = "demo:counter"
STATIC = Path(__file__).parent.parent / "static"
template_engine = JinjaTemplateEngine(directory=Path(__file__).parent)
template_config = TemplateConfig(instance=template_engine)
logger = getLogger("examples.litestar")
basicConfig(level=INFO, format="%(levelname)s %(name)s: %(message)s")
logger.setLevel(INFO)
beam: Services | None = None


class Increment(BaseModel):
    amount: Annotated[int, Field(ge=1, le=10)]


def client() -> Services:
    if beam is None:
        message = "Hornbeam application lifespan has not started"
        raise RuntimeError(message)
    return beam


@asynccontextmanager
async def lifespan(app: Litestar) -> AsyncGenerator[None]:
    global beam  # noqa: PLW0603
    beam = await Beam.connect()
    app.stores.register("demo", BeamStore(beam, "litestar:demo"), allow_override=True)
    await app.stores.get("demo").set("notice", "Public BEAM counter demonstration")
    try:
        yield
    finally:
        beam = None


def counter_html(value: int) -> str:
    return template_engine.get_template("counter.html").render(value=value)


@get("/health")
async def health() -> dict[str, str]:
    await client().incr("shared", 0, namespace="demo")
    return {"status": "ok"}


@get("/counter", media_type="text/html")
async def counter() -> Template:
    return Template(
        template_name="counter.html",
        context={"value": await client().incr("shared", 0, namespace="demo")},
    )


@get("/", media_type="text/html")
async def index(request: Request[object, object, State]) -> Template:
    notice = await request.app.stores.get("demo").get("notice")
    title = notice.decode("utf-8") if notice is not None else "Public counter"
    current = await client().incr("shared", 0, namespace="demo")
    return Template(template_name="index.html", context={"title": title, "value": current})


@post("/increment", media_type="text/html", status_code=200)
async def increment(
    request: Request[object, object, State],
    data: Annotated[Increment, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Template:
    origin = request.headers.get("origin")
    expected = request.url.scheme + "://" + request.url.netloc
    if (origin is not None and origin != expected) or request.headers.get("sec-fetch-site") in {
        "cross-site",
        "same-site",
    }:
        raise HTTPException(status_code=403, detail="Cross-origin mutation rejected")
    count = await client().incr("shared", data.amount, namespace="demo")
    logger.info("Incremented shared BEAM counter: amount=%s count=%s", data.amount, count)
    await client().publish(TOPIC, b"changed")
    logger.info("Published invalidation to %s", TOPIC)
    return Template(template_name="confirmation.html", context={"amount": data.amount})


class CounterEvents(ASGIStreamingResponse):
    def __init__(self, owned: Services) -> None:
        super().__init__(
            iterator=(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self.owned = owned

    async def messages(self, subscription: Subscription) -> AsyncGenerator[bytes]:
        while True:
            count = await self.owned.incr("shared", 0, namespace="demo")
            logger.info("Subscription update: fresh BEAM read count=%s", count)
            yield ServerSentEventMessage(data=counter_html(count)).encode()
            await subscription.receive()

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with self.owned.subscribe(TOPIC) as subscription:
            logger.info("Subscription connected to %s", TOPIC)
            messages = self.messages(subscription)
            self.iterator = messages
            try:
                await super().__call__(scope, receive, send)
            finally:
                await messages.aclose()
                logger.info("Subscription disconnected from %s", TOPIC)


@get("/events")
async def events(request: Request[object, object, State]) -> CounterEvents:
    return CounterEvents(client().for_scope(request.scope))


@get("/static/{asset:str}")
async def asset_file(asset: FromPath[str]) -> File | Response[str]:
    if asset not in {"htmx.min.js", "sse.js", "missing.min.css"}:
        return Response("Not found", status_code=404)
    return File(
        STATIC / asset,
        media_type="text/css" if asset.endswith(".css") else "text/javascript",
    )


app = Litestar(
    route_handlers=[health, counter, index, increment, events, asset_file],
    lifespan=[lifespan],
    template_config=template_config,
)
