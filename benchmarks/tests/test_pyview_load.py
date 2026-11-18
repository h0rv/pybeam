import pytest
from pydantic import ValidationError

from benchmarks.web.pyview_load import MESSAGE, Page


def test_page_tokens() -> None:
    page = Page(
        '<meta name="csrf-token" content="abc&amp;def">'
        '<div id="phx-123" data-phx-session="signed"></div>'
    )
    assert (page.topic, page.session, page.csrf) == ("lv:phx-123", "signed", "abc&def")


@pytest.mark.parametrize("html", ["", '<div id="phx-1" data-phx-session="x"></div>'])
def test_missing_tokens(html: str) -> None:
    with pytest.raises(ValueError, match="Missing LiveView"):
        Page(html)


def test_liveview_message() -> None:
    assert MESSAGE.validate_json('["1",null,"lv:phx-1","diff",{"0":"2"}]') == (
        "1",
        None,
        "lv:phx-1",
        "diff",
        {"0": "2"},
    )


@pytest.mark.parametrize("message", ["[]", '[null,null,"topic","diff",[]]', "invalid"])
def test_invalid_message(message: str) -> None:
    with pytest.raises(ValidationError):
        MESSAGE.validate_json(message)
