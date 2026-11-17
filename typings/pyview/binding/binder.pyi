from collections.abc import Callable

from pyview.binding.context import BindContext

class BindResult:
    bound_args: dict[str, object]
    errors: list[object]
    @property
    def success(self) -> bool: ...

class Binder[T]:
    async def abind(self, func: Callable[..., object], ctx: BindContext[T]) -> BindResult: ...
