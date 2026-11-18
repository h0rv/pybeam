from collections.abc import Callable
from typing import Protocol, TypedDict

from markupsafe import Markup

class RenderedContent(Protocol):
    def tree(self) -> dict[str, object]: ...
    def text(self, socket: object | None = None) -> str: ...

class RootTemplateContext(TypedDict):
    id: str
    content: str
    title: str | None
    csrf_token: str
    session: str | None
    additional_head_elements: list[Markup]

type RootTemplate = Callable[[RootTemplateContext], str]

def defaultRootTemplate(
    css: Markup | None = None,
    content_wrapper: Callable[[RootTemplateContext, Markup], Markup] | None = None,
    title: str | None = None,
    title_suffix: str | None = " | LiveView",
    head_content: Markup | None = None,
) -> RootTemplate: ...
