import pytest
from pydantic import ValidationError
from pyview.events import InfoEvent
from pyview.live_socket import UnconnectedSocket

from examples.pyview_cluster.main import TOPIC, FeedState, FeedView, Post, notification
from tests.fakes import FakeServices, ServiceState


async def test_feed__starts_empty_and_publishes_trimmed_post() -> None:
    view = FeedView()
    socket = UnconnectedSocket[FeedState]()
    await view.mount(socket, {})
    assert socket.context.posts == []
    assert view.subscription is None
    state = ServiceState()
    beam = FakeServices(state, owner="test")
    await view.handle_event("send", socket, "  hello <world>  ", beam=beam)
    topic, payload = state.published[0]
    assert topic == TOPIC
    assert Post.model_validate_json(payload) == Post(text="hello <world>", node="A")
    assert socket.context.posts == []
    await view.disconnect(socket)


@pytest.mark.parametrize("text", ["", "   ", "x" * 281])
async def test_feed__invalid_post_does_not_publish(text: str) -> None:
    view = FeedView()
    socket = UnconnectedSocket[FeedState]()
    await view.mount(socket, {})
    state = ServiceState()
    await view.handle_event("send", socket, text, beam=FakeServices(state, owner="test"))
    assert socket.context.error == "Enter a post from 1 to 280 characters."
    assert not state.published


async def test_feed__keeps_latest_twenty_and_rejects_unexpected_events() -> None:
    view = FeedView()
    socket = UnconnectedSocket[FeedState]()
    await view.mount(socket, {})
    for index in range(25):
        post = Post(text=str(index), node="B")
        await view.handle_info(notification(post.model_dump_json().encode()), socket)
    assert [post.text for post in socket.context.posts] == [str(i) for i in range(5, 25)]
    with pytest.raises(ValueError, match="unexpected feed notification"):
        await view.handle_info(InfoEvent("other"), socket)
    with pytest.raises(ValueError, match="unexpected feed event"):
        await view.handle_event("other", socket, beam=FakeServices(ServiceState(), owner="test"))


@pytest.mark.parametrize("payload", [b"", b"not json", b'{"text":"hi","node":"C"}'])
def test_feed__invalid_payload_is_rejected(payload: bytes) -> None:
    with pytest.raises(ValidationError):
        notification(payload)
