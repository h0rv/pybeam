from collections.abc import Generator, Mapping
from contextlib import contextmanager
from types import TracebackType
from typing import Self

from pybeam.integrations.hornbeam.contracts import ByteStore, Services, Subscription
from pybeam.integrations.hornbeam.errors import SubscriptionClosed, SubscriptionOverflow


@contextmanager
def _subscription_errors() -> Generator[None]:
    from hornbeam.api.errors import SubscriptionClosed as NativeClosed
    from hornbeam.api.errors import SubscriptionOverflow as NativeOverflow

    try:
        yield
    except NativeClosed as error:
        raise SubscriptionClosed(str(error)) from error
    except NativeOverflow as error:
        raise SubscriptionOverflow(str(error)) from error


class _Subscription:
    def __init__(self, native: Subscription) -> None:
        self._native = native

    async def __aenter__(self) -> Self:
        with _subscription_errors():
            await self._native.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        with _subscription_errors():
            await self._native.__aexit__(exc_type, exc, traceback)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        with _subscription_errors():
            return await anext(self._native)

    async def receive(self) -> bytes:
        with _subscription_errors():
            return await self._native.receive()

    async def aclose(self) -> None:
        with _subscription_errors():
            await self._native.aclose()


class Beam:
    """Hornbeam services with public subscription errors translated at this boundary.

    Scope selection and cleanup remain owned by Hornbeam. Other native errors are
    unchanged, including protocol failures and cleanup acknowledgement timeouts.
    """

    def __init__(self, native: Services) -> None:
        self._native = native

    @classmethod
    async def connect(cls) -> Beam:
        from hornbeam.services.client import Beam as NativeBeam

        return cls(await NativeBeam.connect())

    def for_scope(self, scope: Mapping[str, object]) -> Beam:
        return Beam(self._native.for_scope(scope))

    def store(self, namespace: str) -> ByteStore:
        return self._native.store(namespace)

    async def incr(self, key: str, delta: int = 1, *, namespace: str = "default") -> int:
        return await self._native.incr(key, delta, namespace=namespace)

    async def publish(self, topic: str, payload: bytes) -> int:
        return await self._native.publish(topic, payload)

    def subscribe(self, topic: str, *, max_bytes: int = 65536) -> Subscription:
        with _subscription_errors():
            return _Subscription(self._native.subscribe(topic, max_bytes=max_bytes))

    async def subscription_count(self) -> int:
        return await self._native.subscription_count()
