from collections.abc import AsyncGenerator
from logging import INFO, basicConfig, getLogger
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pybeam.services import Services, Subscription
from pybeam.fastapi.dependencies import Beam, provider

_TOPIC = "demo:counter"
logger = getLogger("examples.fastapi")
basicConfig(level=INFO, format="%(levelname)s %(name)s: %(message)s")
logger.setLevel(INFO)

templates = Jinja2Templates(directory=Path(__file__).parent)
app = FastAPI(lifespan=provider.lifespan)
counter_subscription = provider.subscription(_TOPIC)
app.mount("/static", StaticFiles(directory=Path(__file__).parent.parent / "static"), name="static")


def counter_html(count: int) -> str:
    return templates.get_template("counter.html").render(count=count)


def require_same_origin(request: Request) -> None:
    if request.headers.get("sec-fetch-site") not in (None, "none", "same-origin"):
        raise HTTPException(403, "Cross-origin mutations are not allowed")
    expected = request.url.scheme + "://" + request.url.netloc
    origin = request.headers.get("origin")
    if origin is not None and origin != expected:
        raise HTTPException(403, "Cross-origin mutations are not allowed")
    referer = request.headers.get("referer")
    if referer is not None:
        try:
            parsed = urlsplit(referer)
        except ValueError as exc:
            raise HTTPException(403, "Invalid Referer") from exc
        if parsed.scheme + "://" + parsed.netloc != expected:
            raise HTTPException(403, "Cross-origin mutations are not allowed")


async def counter_events(
    beam: Services, subscription: Subscription
) -> AsyncGenerator[ServerSentEvent]:
    logger.info("Subscription connected to %s; reading shared counter", _TOPIC)
    try:
        last = await beam.incr("shared", 0, namespace="demo")
        yield ServerSentEvent(raw_data=counter_html(last))
        async for _payload in subscription:
            count = await beam.incr("shared", 0, namespace="demo")
            if count != last:
                last = count
                logger.info("Subscription update: fresh BEAM read count=%s", count)
                yield ServerSentEvent(raw_data=counter_html(count))
    finally:
        logger.info("Subscription disconnected from %s", _TOPIC)


@app.get("/health", response_class=PlainTextResponse)
async def health(beam: Beam) -> str:
    await beam.incr("shared", 0, namespace="demo")
    return "ok"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, beam: Beam) -> HTMLResponse:
    count = await beam.incr("shared", 0, namespace="demo")
    return templates.TemplateResponse(request=request, name="index.html", context={"count": count})


@app.get("/counter", response_class=HTMLResponse)
async def counter(request: Request, beam: Beam) -> HTMLResponse:
    count = await beam.incr("shared", 0, namespace="demo")
    return templates.TemplateResponse(
        request=request, name="counter.html", context={"count": count}
    )


@app.post("/increment", response_model=None, dependencies=[Depends(require_same_origin)])
async def increment(
    request: Request,
    beam: Beam,
    amount: Annotated[int, Form(ge=1, le=10)],
) -> Response:
    count = await beam.incr("shared", amount, namespace="demo")
    logger.info("Incremented shared BEAM counter: amount=%s count=%s", amount, count)
    await beam.publish(_TOPIC, str(count).encode("ascii"))
    logger.info("Published invalidation to %s; subscribers will read BEAM state", _TOPIC)
    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(
            request=request, name="confirmation.html", context={"amount": amount}
        )
    return RedirectResponse(request.url_for("index"), status_code=303)


@app.get("/events", response_class=EventSourceResponse)
async def events(
    beam: Beam,
    subscription: Annotated[Subscription, Depends(counter_subscription, scope="request")],
) -> AsyncGenerator[ServerSentEvent]:
    stream = counter_events(beam, subscription)
    try:
        async for event in stream:
            yield event
    finally:
        await stream.aclose()
