from string.templatelib import Template

from pyview.meta import PyViewMeta
from pyview.template import RenderedContent

class TemplateView[T]:
    async def render(self, assigns: T, meta: PyViewMeta) -> RenderedContent: ...
    def template(self, assigns: T, meta: PyViewMeta) -> Template: ...
