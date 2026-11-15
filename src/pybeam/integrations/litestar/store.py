from datetime import timedelta
from typing import TYPE_CHECKING, override

from litestar.stores.base import Store

if TYPE_CHECKING:
    from pybeam.integrations.hornbeam import ByteStore, Services


def _seconds(duration: int | timedelta | None) -> float | None:
    if duration is None:
        return None
    return duration.total_seconds() if isinstance(duration, timedelta) else float(duration)


class BeamStore(Store):
    """UTF-8 Store backed by one exact BEAM namespace (not a NamespacedStore).

    Zero expires immediately; negative durations are rejected by ByteStore.
    Renewal is atomic on the service and never makes a persistent entry expire.
    TTL inspection truncates fractional remaining seconds, matching Store's int API.
    Missing and persistent entries both return None.
    """

    def __init__(self, beam: Services, namespace: str) -> None:
        self._store: ByteStore = beam.store(namespace)

    @override
    async def set(
        self, key: str, value: str | bytes, expires_in: int | timedelta | None = None
    ) -> None:
        await self._store.set(
            key,
            value.encode("utf-8") if isinstance(value, str) else value,
            expires_in=_seconds(expires_in),
        )

    @override
    async def get(self, key: str, renew_for: int | timedelta | None = None) -> bytes | None:
        return await self._store.get(key, renew_for=_seconds(renew_for))

    @override
    async def delete(self, key: str) -> None:
        await self._store.delete(key)

    @override
    async def delete_all(self) -> None:
        await self._store.delete_all()

    @override
    async def exists(self, key: str) -> bool:
        return await self._store.exists(key)

    @override
    async def expires_in(self, key: str) -> int | None:
        remaining = await self._store.expires_in(key)
        return None if remaining is None else int(remaining)
