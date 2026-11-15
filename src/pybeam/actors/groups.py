import asyncio
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from itertools import count
from types import TracebackType
from typing import Protocol, Self, TypeIs, overload

from pydantic import BaseModel, ConfigDict, PositiveInt

from pybeam.actors.classes import (
    ActorStoppedError,
    Container,
    DataActor,
    Reference,
    Route,
    definition,
    reference_route,
)
from pybeam.actors.codec import CONSTRUCTION_PROBE, ActorPayloadError, decode_reply


class DataGroup(Protocol):
    async def spawn(
        self,
        child_id: str,
        factory: Callable[[], Container],
        *,
        capacity: int = 64,
        max_bytes: int = 1_048_576,
    ) -> DataActor: ...

    async def stop(self, child_id: str, /) -> None: ...
    async def close(self) -> None: ...


async def open_group(*, max_children: int) -> DataGroup:
    try:
        from erlang.actors import DataActorGroup
    except ImportError as error:
        raise RuntimeError(
            "Bounded native actor groups are required; no raw-object fallback"
        ) from error
    return await DataActorGroup(max_children=max_children).__aenter__()


@dataclass(frozen=True, slots=True)
class Started[T]:
    reference: T


@dataclass(frozen=True, slots=True)
class Failed:
    error: BaseException


def is_actor_class[T](value: Callable[..., T]) -> TypeIs[type[T]]:
    return isinstance(value, type)


class _ActorGroupConfig(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    max_children: PositiveInt
    max_pending: PositiveInt
    max_bytes: PositiveInt


class ActorGroup:
    """Own native OTP actors within one VM.

    Limits bound admitted requests and encoded bytes, not Python heap or RSS.
    Encoded replies are size-checked too. Exit destroys the owned native contexts;
    asynchronous library resources still require explicit application cleanup.
    """

    def __init__(
        self, *, max_children: int = 128, max_pending: int = 64, max_bytes: int = 1_048_576
    ) -> None:
        self._limits = _ActorGroupConfig(
            max_children=max_children, max_pending=max_pending, max_bytes=max_bytes
        )
        self._stack = AsyncExitStack()
        self._starts: asyncio.TaskGroup | None = None
        self._native: DataGroup | None = None
        self._routes: list[Route] = []
        self._ids = count()
        self._stops: dict[str, asyncio.Task[Failed | None]] = {}
        self._entered = False
        self._closing = False
        self._closing_task: asyncio.Task[bool | None] | None = None

    async def __aenter__(self) -> Self:
        if self._entered or self._closing:
            raise RuntimeError("ActorGroup cannot be entered twice or after closing")
        self._entered = True
        native = await open_group(max_children=self._limits.max_children)
        self._native = native
        self._stack.push_async_callback(native.close)
        try:
            self._starts = await asyncio.TaskGroup().__aenter__()
            self._stack.push_async_callback(self._starts.__aexit__, None, None, None)
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def _spawn[T](
        self,
        cls: type[T],
        native: DataGroup,
        args: tuple[object, ...],
        kwargs: Mapping[str, object],
    ) -> Started[T] | Failed:
        try:
            spec = definition(cls)
            payload = spec.constructor.encode(args, kwargs)
            capacity = self._limits.max_pending
            size = self._limits.max_bytes
            if len(payload) > size:
                raise ActorPayloadError("Encoded actor constructor exceeds max_bytes")
            actor = await native.spawn(
                str(next(self._ids)),
                spec.factory(payload, max_bytes=size),
                capacity=capacity,
                max_bytes=size,
            )
            try:
                decode_reply(await actor.call(CONSTRUCTION_PROBE))
            except BaseException:
                await native.stop(actor.child_id)
                raise
            route = Route(actor, spec.methods, cls, active=not self._closing)
            self._routes.append(route)
            return Started(spec.reference(cls, route))
        except BaseException as error:
            return Failed(error)

    @overload
    async def spawn[T](self, cls: Callable[[], T], /) -> T: ...

    @overload
    async def spawn[T, **P](
        self, cls: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
    ) -> T: ...

    async def spawn[T, **P](self, cls: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        """Construct once inside native ownership; concurrent child tasks may spawn.

        Cancelling a call does not undo or retry an actor method. Group shutdown
        revokes all references and waits for admitted starts before native cleanup.
        """
        if self._closing or self._starts is None or self._native is None:
            raise RuntimeError("ActorGroup is not open")
        if not is_actor_class(cls):
            raise TypeError("ActorGroup.spawn requires a decorated actor class")
        outcome = await self._starts.create_task(self._spawn(cls, self._native, args, kwargs))
        if isinstance(outcome, Failed):
            raise outcome.error
        return outcome.reference

    def _require_member(self, reference: object) -> Route:
        if self._closing or self._native is None or self._starts is None:
            raise RuntimeError("ActorGroup is not open")
        if not isinstance(reference, Reference):
            raise TypeError("Expected an actor reference")
        route = reference_route(reference)
        if not any(member is route for member in self._routes):
            raise ValueError("Actor reference belongs to another group")
        return route

    async def _stop(self, native: DataGroup, child_id: str) -> Failed | None:
        try:
            await native.stop(child_id)
        except BaseException as error:
            return Failed(error)
        return None

    async def stop(self, reference: object, /) -> None:
        route = self._require_member(reference)
        child_id = route.native.child_id
        for member in self._routes:
            if member.native.child_id == child_id:
                member.active = False
        if self._starts is None or self._native is None:
            raise RuntimeError("ActorGroup is not open")
        if child_id not in self._stops:
            self._stops[child_id] = self._starts.create_task(self._stop(self._native, child_id))
        outcome = await asyncio.shield(self._stops[child_id])
        if isinstance(outcome, Failed):
            raise outcome.error

    async def rejoin[T](self, reference: T, /) -> T:
        """Explicitly acquire the current generation, without retrying any method."""
        route = self._require_member(reference)
        if route.native.child_id in self._stops:
            raise ActorStoppedError("The actor is stopped")
        native = await route.native.rejoin()
        decode_reply(await native.call(CONSTRUCTION_PROBE))
        fresh = Route(native, route.methods, route.cls, active=not self._closing)
        self._routes.append(fresh)
        route.active = False
        return definition(route.cls).reference(type(reference), fresh)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self._closing_task is None:
            self._closing = True
            for route in self._routes:
                route.active = False
            self._closing_task = asyncio.create_task(
                self._stack.__aexit__(exc_type, exc, traceback)
            )
        cancelled: asyncio.CancelledError | None = None
        while not self._closing_task.done():
            try:
                await asyncio.shield(self._closing_task)
            except asyncio.CancelledError as error:
                cancelled = error
        result = self._closing_task.result()
        if cancelled is not None:
            raise cancelled
        return result


@asynccontextmanager
async def spawn[T, **P](
    cls: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> AsyncGenerator[T]:
    """Own one actor with the same bounded backend as ActorGroup."""
    async with ActorGroup() as group:
        yield await group.spawn(cls, *args, **kwargs)
