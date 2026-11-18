import os
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from string.templatelib import Template
from typing import TYPE_CHECKING, Annotated, Literal, override

from markupsafe import Markup
from pydantic import BaseModel, Field, ValidationError
from pyview import Depends
from pyview.events import InfoEvent
from pyview.live_socket import LiveViewSocket, is_connected
from pyview.live_view import LiveView, Session
from pyview.pyview import PyView
from pyview.template import defaultRootTemplate
from pyview.template.template_view import TemplateView
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from pybeam.integrations.hornbeam import Services
from pybeam.integrations.pyview.dependencies import services
from pybeam.integrations.pyview.subscriptions import ViewSubscription

if TYPE_CHECKING:
    from pyview.meta import PyViewMeta
    from starlette.requests import Request

TOPIC = "demo:feed"
NODE = os.environ.get("PYBEAM_FEED_NODE", "A")


class Message(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=280)]


class Post(Message):
    node: Literal["A", "B"]


@dataclass
class FeedState:
    posts: list[Post] = field(default_factory=list)
    error: str = ""


def notification(payload: bytes) -> InfoEvent:
    return InfoEvent(TOPIC, Post.model_validate_json(payload))


class FeedView(TemplateView[FeedState], LiveView[FeedState]):
    def __init__(self) -> None:
        super().__init__()
        self.subscription: ViewSubscription | None = None

    @override
    async def mount(
        self,
        socket: LiveViewSocket[FeedState],
        session: Session,
    ) -> None:
        socket.context = FeedState()
        if is_connected(socket):
            self.subscription = await services.subscribe(socket, TOPIC, notification)

    async def handle_event(
        self,
        event: str,
        socket: LiveViewSocket[FeedState],
        text: str = "",
        beam: Services = Depends(services),
    ) -> None:
        if event != "send":
            raise ValueError("unexpected feed event")
        try:
            message = Message(text=text.strip())
        except ValidationError:
            socket.context.error = "Enter a post from 1 to 280 characters."
            return
        post = Post.model_validate({"text": message.text, "node": NODE})
        await beam.publish(TOPIC, post.model_dump_json().encode())
        socket.context.error = ""

    @override
    async def handle_info(self, event: InfoEvent, socket: LiveViewSocket[FeedState]) -> None:
        if event.name != TOPIC or not isinstance(event.payload, Post):
            raise ValueError("unexpected feed notification")
        socket.context.posts.append(event.payload)
        del socket.context.posts[:-20]

    @override
    async def disconnect(self, socket: LiveViewSocket[FeedState]) -> None:
        if self.subscription is not None:
            await self.subscription.aclose()

    @override
    def template(self, assigns: FeedState, meta: PyViewMeta) -> Template:
        posts = [
            t"<li><p>{post.text}</p><small>From node {post.node}</small></li>"
            for post in assigns.posts
        ]
        empty = t"<p>No posts received in this tab yet.</p>" if not posts else t""
        return t"""<main>
            <h1>Two-node feed</h1>
            <p>This tab is served by node {NODE}.</p>
            <form phx-submit="send">
                <label for="post">Post</label>
                <input id="post" name="text" type="text" maxlength="280" required autocomplete="off">
                <button type="submit">Send</button>
            </form>
            <p role="status">{assigns.error}</p>
            <h2>Posts received in this tab</h2>
            {empty}
            <ol aria-live="polite">{posts}</ol>
            <p>Each tab keeps its latest 20 posts. New tabs start empty. Nothing is saved.</p>
        </main>"""


async def health(_request: Request) -> Response:
    return PlainTextResponse("ok")


app = PyView(
    lifespan=services.lifespan,
    routes=[
        Route("/health", health),
        Mount("/static", StaticFiles(directory=str(files("pyview").joinpath("static")))),
        Mount("/feed-static", StaticFiles(directory=Path(__file__).parent / "static")),
    ],
)
app.rootTemplate = defaultRootTemplate(
    css=Markup('<link rel="stylesheet" href="/feed-static/feed.css">'),
    title="Two-node feed",
    title_suffix="",
)
app.add_live_view("/", FeedView)
