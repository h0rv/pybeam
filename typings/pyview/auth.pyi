from collections.abc import Callable, Sequence

def requires[T](
    scopes: str | Sequence[str],
    status_code: int = 403,
    redirect: str | None = None,
) -> Callable[[type[T]], type[T]]: ...
