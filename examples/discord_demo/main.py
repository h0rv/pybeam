import argparse
import asyncio
import hashlib
import hmac
import logging
import os
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn import Config, Server

from examples.discord_demo.domain import ROOMS, Action, Demo, View
from pybeam.actors import ActorCallError, ActorGroup, ActorStoppedError, context_counts
from pybeam.integrations.hornbeam.client import Beam
from pybeam.integrations.hornbeam.contracts import Subscription
from pybeam.integrations.hornbeam.errors import SubscriptionClosed, SubscriptionOverflow
from pybeam.integrations.hornbeam.runtime import run

ROOT = Path(__file__).parent
logger = logging.getLogger("discord_demo")
templates = Jinja2Templates(directory=ROOT / "templates")
type RoomId = Literal["general", "python", "beam", "random"]
type EpochText = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,18})$")]


class Command(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    action: Action
    generation: EpochText
    revision: EpochText | None = None
    csrf: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    text: Annotated[str, Field(max_length=256)] = ""

    @field_validator("generation", "revision")
    @classmethod
    def bounded_epoch(cls, value: str | None) -> str | None:
        if value is not None and int(value) > 9_223_372_036_854_775_807:
            raise ValueError("Epoch or revision exceeds the native integer limit")
        return value

    @model_validator(mode="after")
    def captured_leave_revision(self) -> Self:
        if self.action == "leave" and self.revision is None:
            raise ValueError("Leave requires the membership revision captured in its form")
        return self

    @field_validator("text")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 1024:
            raise ValueError("Message exceeds 1024 UTF-8 bytes")
        return value


@dataclass(frozen=True, slots=True)
class ActionResult:
    status: Literal["confirmed", "unconfirmed", "unknown"]
    message: str


class LocalEmbedOrigin(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    port: Annotated[int, Field(ge=1, le=65535)]

    @property
    def url(self) -> str:
        return "http://127.0.0.1:" + str(self.port)

    @classmethod
    def parse(cls, value: str) -> Self:
        try:
            parsed = urlsplit(value)
            port = parsed.port
            if port is None:
                raise ValueError("Explicit port required")
            origin = cls(port=port)
            if value != origin.url:
                raise ValueError("Only an exact HTTP 127.0.0.1 origin is supported")
            return origin
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                "Expected http://127.0.0.1:PORT with port 1 to 65535 and no path"
            ) from error


@dataclass(slots=True)
class Owner:
    origin: str = "http://127.0.0.1:8766"
    embed_origin: LocalEmbedOrigin | None = None
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    demo: Demo | None = None
    tasks: asyncio.TaskGroup | None = None

    def signature(self, value: str) -> str:
        return hmac.new(self.secret, value.encode("ascii"), hashlib.sha256).hexdigest()

    def identity(self, cookie: str | None) -> str | None:
        if cookie is None or len(cookie) != 97:
            return None
        identity, separator, signature = cookie.partition(".")
        if (
            not separator
            or len(identity) != 32
            or any(c not in "0123456789abcdef" for c in identity + signature)
        ):
            return None
        return identity if hmac.compare_digest(signature, self.signature(identity)) else None

    def csrf(self, identity: str) -> str:
        return self.signature("csrf:" + identity)


owner = Owner()


class LocalOnly:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        headers = request.headers
        reason: str | None = None
        status = 403
        embedded_navigation = (
            owner.embed_origin is not None
            and headers.get("sec-fetch-dest") in ("iframe", "document")
            and headers.get("sec-fetch-mode") == "navigate"
            and (
                (
                    request.method == "GET"
                    and headers.get("sec-fetch-site") in ("same-site", "same-origin")
                    and request.url.path in ("/", *("/rooms/" + room for room in ROOMS))
                )
                or (
                    request.method == "POST"
                    and headers.get("sec-fetch-dest") == "iframe"
                    and headers.get("sec-fetch-site") == "same-origin"
                    and headers.get("origin") == owner.origin
                    and request.url.path in ("/rooms/" + room + "/actions" for room in ROOMS)
                )
            )
        )
        if headers.get("host") != owner.origin.removeprefix("http://"):
            reason = "Unexpected Host"
        elif headers.get("sec-fetch-dest") == "iframe" and not embedded_navigation:
            reason = "Frame navigation rejected"
        elif (
            headers.get("sec-fetch-site") not in (None, "none", "same-origin")
            and not embedded_navigation
        ):
            reason = "Cross-site request rejected"
        elif headers.get("origin") not in (None, owner.origin):
            reason = "Unexpected Origin"
        elif request.method == "POST":
            length = headers.get("content-length", "")
            if headers.get("origin") != owner.origin:
                reason = "Same-origin POST required"
            elif not length.isascii() or not length.isdecimal():
                reason, status = "Content-Length required", 411
            elif int(length) > 4096:
                reason, status = "Form exceeds 4096 bytes", 413
            elif headers.get("transfer-encoding") is not None:
                reason = "Chunked forms are not supported"
        if reason:
            html = templates.get_template("error.html").render(message=reason, origin=owner.origin)
            await HTMLResponse(html, status_code=status)(scope, receive, send)
            return

        streaming = False

        async def secure_send(message: Message) -> None:
            nonlocal streaming
            if message["type"] == "http.response.start":
                streaming = any(
                    name == b"content-type" and value.startswith(b"text/event-stream")
                    for name, value in message["headers"]
                )
                message["headers"] = [
                    *message["headers"],
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; "
                        b"style-src 'self'; img-src 'self' data:; object-src 'none'; "
                        + b"frame-ancestors "
                        + (
                            owner.embed_origin.url.encode("ascii")
                            if owner.embed_origin is not None
                            else b"'none'"
                        )
                        + b"; base-uri 'none'; form-action 'self'",
                    ),
                ]
            if streaming:
                try:
                    async with asyncio.timeout(5):
                        await send(message)
                except TimeoutError:
                    logger.warning("Slow SSE peer exceeded the five-second send deadline")
                    raise
            else:
                await send(message)

        await self.app(scope, receive, secure_send)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    from erlang import ErlangEventLoop

    if not isinstance(asyncio.get_running_loop(), ErlangEventLoop):
        raise RuntimeError("Run this demo with pybeam.run; no ordinary Python-loop fallback")
    try:
        services = await Beam.connect()
        baseline = await services.subscription_count()
        async with services.subscribe("discord:startup", max_bytes=4096):
            pass
        if await services.subscription_count() != baseline:
            raise RuntimeError("Startup subscription did not return to baseline")
    except Exception as error:
        raise RuntimeError(
            "Discord demo could not acquire native Hornbeam services in the managed VM. "
            "Use a runtime with opt-in supervised services and callback registration. "
            "No polling or Python-state fallback is enabled. Native error: " + str(error)
        ) from error
    contexts_before = await context_counts()
    async with ActorGroup(max_children=4, max_pending=8, max_bytes=65536) as group:
        demo = Demo(group, services)
        await demo.start()
        async with asyncio.TaskGroup() as tasks:
            owner.demo, owner.tasks = demo, tasks
            try:
                yield
            finally:
                owner.demo, owner.tasks = None, None
        if demo.streams:
            raise RuntimeError("Native stream owners did not drain")
        if await services.subscription_count() != baseline:
            raise RuntimeError("Native subscriptions did not return to baseline")
    if (await context_counts()).live != contexts_before.live:
        raise RuntimeError("Native room contexts did not return to baseline")


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(LocalOnly)
app.mount("/static", StaticFiles(directory=ROOT.parent / "static"), name="static")
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")


async def runtime() -> Demo:
    if owner.demo is None:
        raise HTTPException(503, "Native demo runtime is unavailable")
    return owner.demo


type Native = Annotated[Demo, Depends(runtime)]


async def member(request: Request) -> str:
    identity = owner.identity(request.cookies.get("discord"))
    if identity is None:
        raise HTTPException(401, "Open a room page to establish an anonymous session")
    return identity


type Member = Annotated[str, Depends(member)]


async def panel(view: View, identity: str, demo: Demo) -> str:
    membership = next(
        (item for item in view.snapshot.ledger if item.member_id == identity and item.present),
        None,
    )
    return templates.get_template("room.html").render(
        view=view,
        member=identity,
        csrf=owner.csrf(identity),
        is_owner=identity == demo.owner,
        revision=membership.revision if membership is not None else None,
        joined=membership is not None,
        nodes=await demo.tree(),
        restart=demo.restarts.get(view.snapshot.room_id),
        server_pid=os.getpid(),
    )


@app.get("/", response_class=RedirectResponse)
async def index() -> str:
    return "/rooms/general"


@app.get("/health", response_class=PlainTextResponse)
async def health(demo: Native) -> str:
    await demo.view("general")
    return "native-room-ok"


class Resources(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    streams: int
    subscriptions: int
    operations: int
    live_contexts: int


@app.get("/resources")
async def resources(demo: Native, identity: Member) -> Resources:
    if identity != demo.owner:
        raise HTTPException(403, "Local resource diagnostics belong to the first browser")
    return Resources(
        streams=demo.streams,
        subscriptions=await demo.services.subscription_count(),
        operations=demo.inflight,
        live_contexts=(await context_counts()).live,
    )


@app.get("/rooms/{room_id}", response_class=HTMLResponse)
async def room_page(
    request: Request,
    room_id: RoomId,
    demo: Native,
    notice: Literal["confirmed", "unconfirmed", "unknown"] | None = None,
) -> HTMLResponse:
    identity = owner.identity(request.cookies.get("discord")) or secrets.token_hex(16)
    if demo.owner is None:
        demo.owner = identity
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "rooms": ROOMS,
            "room_id": room_id,
            "member": identity,
            "room_html": await panel(await demo.view(room_id), identity, demo),
            "notice": notice,
        },
        headers={"Cache-Control": "no-store"},
    )
    response.set_cookie(
        "discord",
        identity + "." + owner.signature(identity),
        httponly=True,
        samesite="strict",
        max_age=3600,
    )
    return response


@app.post("/rooms/{room_id}/actions", response_class=HTMLResponse)
async def action(
    request: Request,
    room_id: RoomId,
    demo: Native,
    identity: Member,
    command: Annotated[Command, Form()],
) -> Response:
    if not hmac.compare_digest(command.csrf, owner.csrf(identity)):
        raise HTTPException(403, "Invalid CSRF token")
    if command.action == "send" and not command.text.strip():
        raise HTTPException(422, "Write a message first")
    if command.action == "crash" and identity != demo.owner:
        raise HTTPException(403, "Crash diagnostic belongs to the first local browser")
    if owner.tasks is None or demo.closing or demo.inflight >= 16:
        raise HTTPException(503, "Native operation admission is full")

    async def complete() -> ActionResult:
        try:
            await demo.change(
                room_id,
                identity,
                int(command.generation),
                command.action,
                command.text,
                secrets.token_hex(16),
                revision=int(command.revision) if command.revision is not None else None,
            )
            return ActionResult("confirmed", "Confirmed " + command.action + ".")
        except (ValueError, ActorCallError, ActorStoppedError) as error:
            return ActionResult(
                "unconfirmed", "Not confirmed: " + str(error) + ". No automatic retry was made."
            )
        except Exception:
            logger.exception("Native operation or notification failed")
            return ActionResult(
                "unknown",
                "Outcome unknown. The call may have effects. Check the room before retrying.",
            )
        finally:
            demo.inflight -= 1

    demo.inflight += 1
    task = owner.tasks.create_task(complete())
    result = await asyncio.shield(task)
    if request.headers.get("hx-request") != "true":
        return RedirectResponse("/rooms/" + room_id + "?notice=" + result.status, status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="ack.html",
        context={"message": result.message},
        headers={"Cache-Control": "no-store"},
    )


async def stream_owner(
    room_id: RoomId, demo: Native, _identity: Member
) -> AsyncGenerator[Subscription]:
    if demo.closing or demo.streams >= 32:
        raise HTTPException(503, "Native stream admission is closed or full")
    demo.streams += 1
    demo.streams_drained.clear()
    try:
        async with demo.services.subscribe("discord:" + room_id, max_bytes=4096) as subscription:
            if demo.closing:
                raise HTTPException(503, "Native stream admission is closed")
            demo.subscriptions.add(subscription)
            try:
                yield subscription
            finally:
                demo.subscriptions.remove(subscription)
    finally:
        demo.streams -= 1
        if demo.streams == 0:
            demo.streams_drained.set()


type Push = Annotated[Subscription, Depends(stream_owner, scope="request")]


@app.get("/rooms/{room_id}/events", response_class=EventSourceResponse)
async def events(
    room_id: RoomId, demo: Native, identity: Member, subscription: Push
) -> AsyncGenerator[ServerSentEvent]:
    try:
        yield ServerSentEvent(raw_data=await panel(await demo.view(room_id), identity, demo))
        async for _notification in subscription:
            yield ServerSentEvent(raw_data=await panel(await demo.view(room_id), identity, demo))
    except (SubscriptionClosed, SubscriptionOverflow) as error:
        logger.warning("Native stream closed: %s", error)
        html = templates.get_template("stream_error.html").render(message=str(error))
        yield ServerSentEvent(raw_data=html)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Local native Discord room miniapp")
    parser.add_argument("--port", type=int, default=8766, choices=range(1024, 65536))
    parser.add_argument(
        "--embed-origin",
        type=LocalEmbedOrigin.parse,
        default=None,
        help="Allow framing by one exact local origin, e.g. http://127.0.0.1:38921",
    )
    args = parser.parse_args()
    owner.origin = "http://127.0.0.1:" + str(args.port)
    owner.embed_origin = args.embed_origin
    logging.basicConfig(level=logging.INFO)
    server = Server(
        Config(
            app,
            host="127.0.0.1",
            port=args.port,
            loop="none",
            http="h11",
            limit_concurrency=64,
            lifespan="on",
            timeout_graceful_shutdown=1,
        )
    )
    try:
        await server.serve()
    except asyncio.CancelledError:
        if owner.demo is not None:
            await owner.demo.close_streams()
        if server.started:
            await server.shutdown()
        raise
    if not server.started:
        raise RuntimeError("Discord demo could not start; see the native capability error above")


if __name__ == "__main__":
    run(main)
