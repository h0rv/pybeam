# Narrow stubs for pyview-web d29dda1; only APIs used by this project.

from collections.abc import Callable, Coroutine
from typing import overload

@overload
def Depends[T](
    dependency: Callable[..., Coroutine[object, object, T]], use_cache: bool = True
) -> T: ...
@overload
def Depends[T](dependency: Callable[..., T], use_cache: bool = True) -> T: ...
