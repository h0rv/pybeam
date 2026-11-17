import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import wraps
from threading import get_ident
from types import FunctionType
from typing import Never, Protocol, dataclass_transform, get_type_hints, override

from pybeam.actors.codec import (
    CONSTRUCTION_PROBE,
    ActorPayloadError,
    Arguments,
    Defaults,
    Method,
    decode_reply,
    encode_failure,
)
from pybeam.actors.state import Snapshot, SnapshotSpec


class ActorStateAccessError(AttributeError):
    """References expose async methods, not actor-owned state fields."""


class ActorStoppedError(RuntimeError):
    """The reference's owning group is closed."""


@dataclass(frozen=True, slots=True)
class Constructing:
    thread: int


class State:
    __slots__ = ("_actor_owner", "_actor_snapshot")

    @override
    def __getattribute__(self, name: str) -> object:
        require_state(self)
        if name in {"_actor_owner", "_actor_snapshot"}:
            raise ActorStateAccessError("Actor ownership is private")
        return object.__getattribute__(self, name)

    @override
    def __setattr__(self, name: str, value: object) -> None:
        require_state(self)
        if name in {"_actor_owner", "_actor_snapshot"}:
            raise ActorStateAccessError("Actor ownership is private")
        object.__setattr__(self, name, value)

    @override
    def __delattr__(self, name: str) -> None:
        require_state(self)
        if name in {"_actor_owner", "_actor_snapshot"}:
            raise ActorStateAccessError("Actor ownership is private")
        object.__delattr__(self, name)


def require_state(state: State) -> None:
    owner: object = object.__getattribute__(state, "_actor_owner")
    if isinstance(owner, Constructing) and owner.thread == get_ident():
        return
    if isinstance(owner, asyncio.Task) and owner is asyncio.current_task():
        return
    raise ActorStateAccessError("Actor state belongs only to its current native dispatch task")


async def write_state(state: object, /) -> int:
    """Save all declared fields and return the new version, starting at one.

    Call inside a method of an actor started with ActorGroup.restore. Unsaved
    changes are lost on restart. This does not commit external side effects.
    Cancellation waits for the write outcome; storage errors require restart.
    """
    if not isinstance(state, State):
        raise ActorStateAccessError("write_state requires actor-owned state")
    require_state(state)
    snapshot: object = object.__getattribute__(state, "_actor_snapshot")
    if not isinstance(snapshot, Snapshot):
        raise ActorStateAccessError("ActorGroup.restore is required for state writes")
    return await snapshot.write(state)


class DataActor(Protocol):
    child_id: str

    async def call(self, payload: bytes, /) -> bytes: ...
    async def rejoin(self) -> DataActor: ...


@dataclass(slots=True)
class Route:
    native: DataActor
    methods: tuple[Method, ...]
    cls: type[object]
    active: bool = True

    async def invoke(
        self,
        implementation: Callable[..., Awaitable[object]],
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        if not self.active:
            raise ActorStoppedError("The actor group is closed")
        index = next(
            (
                index
                for index, method in enumerate(self.methods)
                if method.function is implementation
            ),
            None,
        )
        if index is None:
            raise ValueError("Actor method implementation is not registered for this reference")
        method = self.methods[index]
        # This synchronous snapshot happens before native admission can suspend.
        payload = index.to_bytes(4) + method.arguments.encode(args, kwargs)
        return method.decode(decode_reply(await self.native.call(payload)))


@dataclass(frozen=True, slots=True)
class StateField:
    name: str

    def __get__(self, instance: object, owner: type[object] | None = None) -> Never:
        raise ActorStateAccessError("Actor references do not expose state: " + self.name)

    def __set__(self, instance: object, value: object) -> Never:
        raise ActorStateAccessError("Actor references do not allow state writes: " + self.name)


class Reference:
    __slots__ = ("_actor_route",)

    def __init__(self, route: Route) -> None:
        object.__setattr__(self, "_actor_route", route)

    @override
    def __getattribute__(self, name: str) -> object:
        route = reference_route(self)
        if name in {"__class__", "__repr__", "__hash__", "__eq__", "__ne__"} or any(
            method.name == name for method in route.methods
        ):
            return object.__getattribute__(self, name)
        raise ActorStateAccessError("Actor references do not expose state: " + name)

    @override
    def __setattr__(self, name: str, value: object) -> None:
        raise ActorStateAccessError("Actor references do not allow state writes: " + name)

    @override
    def __delattr__(self, name: str) -> None:
        raise ActorStateAccessError("Actor references do not allow state deletion: " + name)


def reference_route(reference: Reference) -> Route:
    value: object = object.__getattribute__(reference, "_actor_route")
    if not isinstance(value, Route):
        raise RuntimeError("Invalid actor reference route")
    return value


@dataclass(frozen=True, slots=True)
class Definition:
    cls: type[object]
    constructor: Arguments
    methods: tuple[Method, ...]
    proxy: type[object]
    state: type[object]

    def reference[T](self, cls: type[T], route: Route) -> T:
        # This is a real subclass instance initialized with its opaque route only.
        # User __init__/__post_init__ and resources belong exclusively to native state.
        reference: object = self.proxy(route)
        if not isinstance(reference, cls):
            raise TypeError("Actor reference does not implement the declared class")
        return reference

    def factory(
        self, payload: bytes, *, max_bytes: int = 1_048_576, snapshot: SnapshotSpec | None = None
    ) -> Callable[[], Container]:
        def construct() -> Container:
            try:
                bound = self.constructor.decode(payload)
                state = Constructed(self.state(*bound.args, **bound.kwargs))
            except Exception as error:
                return Container(
                    ConstructionFailed(encode_failure(error, "construction")),
                    self.methods,
                    max_bytes=max_bytes,
                )
            return Container(state, self.methods, max_bytes=max_bytes, snapshot=snapshot)

        return construct


@dataclass(frozen=True, slots=True)
class Constructed:
    state: object


@dataclass(frozen=True, slots=True)
class ConstructionFailed:
    payload: bytes


class Container:
    def __init__(
        self,
        construction: Constructed | ConstructionFailed,
        methods: tuple[Method, ...],
        *,
        max_bytes: int = 1_048_576,
        snapshot: SnapshotSpec | None = None,
    ) -> None:
        self._construction = construction
        self._methods = methods
        self._max_bytes = max_bytes
        self._snapshot = Snapshot(snapshot) if snapshot is not None else None
        self._loaded = False

    async def dispatch(self, payload: bytes) -> bytes:
        if not self._loaded and isinstance(self._construction, Constructed):
            if self._snapshot is not None:
                try:
                    await self._snapshot.restore(self._construction.state)
                    object.__setattr__(self._construction.state, "_actor_snapshot", self._snapshot)
                except Exception as error:
                    self._construction = ConstructionFailed(encode_failure(error, "construction"))
            self._loaded = True
        if isinstance(self._construction, ConstructionFailed):
            reply = self._construction.payload
            return reply if len(reply) <= self._max_bytes else b"\x02"
        if payload == CONSTRUCTION_PROBE:
            return b"\x00"
        state = self._construction.state
        if len(payload) < 4:
            raise ValueError("Invalid actor call header")
        index = int.from_bytes(payload[:4])
        if index >= len(self._methods):
            raise ValueError("Invalid actor method")
        owner: object = object.__getattribute__(state, "_actor_owner")
        if owner is not None:
            raise ActorStateAccessError("Native actor dispatch is already active")
        object.__setattr__(state, "_actor_owner", asyncio.current_task())
        try:
            try:
                reply = b"\x00" + await self._methods[index].dispatch(state, payload[4:])
            except ActorPayloadError as error:
                reply = encode_failure(error, "payload")
            except Exception as error:
                reply = encode_failure(error, "method")
            return reply if len(reply) <= self._max_bytes else b"\x02"
        finally:
            object.__setattr__(state, "_actor_owner", None)


_DEFINITIONS: dict[type[object], Definition] = {}
_ROUTED_METHODS: dict[Callable[..., Awaitable[object]], Method] = {}


def definition(cls: type[object]) -> Definition:
    try:
        return _DEFINITIONS[cls]
    except KeyError as error:
        raise TypeError("Actor classes must be decorated with @pybeam.actor") from error


def routed(method: Method) -> Callable[..., Awaitable[object]]:
    @wraps(method.function)
    async def invoke(state: object, *args: object, **kwargs: object) -> object:
        if isinstance(state, Reference):
            return await reference_route(state).invoke(method.function, args, kwargs)
        if isinstance(state, State):
            require_state(state)
        return await method.function(state, *args, **kwargs)

    _ROUTED_METHODS[invoke] = method
    return invoke


def declared_method(name: str, function: Callable[..., Awaitable[object]]) -> Method:
    original = _ROUTED_METHODS.get(function)
    if original is None:
        return Method.build(name, function)
    return Method(name, original.function, original.arguments, original.result)


@dataclass_transform(field_specifiers=(field,))
def actor[T](cls: type[T]) -> type[T]:
    """Declare dataclass state and a data-only async interface.

    References are initialized subclasses with methods but no local state. Python's
    type system retains field annotations; reading or writing those fields on a
    reference raises ActorStateAccessError at runtime. No shared-object transport
    or custom descriptors/metaclasses are supported. Ownership checks use the
    dispatch task itself, so child tasks cannot inherit permission to use state.
    """
    if type(cls) is not type:
        raise TypeError("Actor classes do not support custom metaclasses")
    declared_names = frozenset(vars(cls))
    members: dict[str, object] = {}
    generated = {
        "__dataclass_fields__",
        "__dataclass_params__",
        "__match_args__",
        "__replace__",
        "__repr__",
        "__eq__",
        "__hash__",
        "__init__",
    }
    for base in reversed(cls.__mro__[:-1]):
        members.update(
            {
                name: member
                for name, member in vars(base).items()
                if base not in _DEFINITIONS or name not in generated
            }
        )
    for name, member in members.items():
        if name in {"_actor_route", "_actor_owner", "_actor_snapshot"}:
            raise TypeError("Reserved actor reference member: " + name)
        if isinstance(member, (property, staticmethod, classmethod)):
            raise TypeError("Actor descriptors are unsupported: " + name)
        if name.startswith("__") and name.endswith("__"):
            if name in {"__init__", "__post_init__"} and inspect.iscoroutinefunction(member):
                raise TypeError("Actor constructors must be synchronous")
            if name not in {
                "__module__",
                "__doc__",
                "__annotations__",
                "__annotations_cache__",
                "__init__",
                "__post_init__",
                "__dict__",
                "__weakref__",
                "__firstlineno__",
                "__static_attributes__",
                "__annotate__",
                "__annotate_func__",
                "__type_params__",
            }:
                raise TypeError("Unsupported actor special member: " + name)
        elif isinstance(member, FunctionType) and not inspect.iscoroutinefunction(member):
            raise TypeError("Actor interface methods must be async: " + name)
        elif inspect.isdatadescriptor(member) or (
            not isinstance(member, FunctionType)
            and inspect.getattr_static(type(member), "__get__", None) is not None
        ):
            raise TypeError("Actor descriptors are unsupported: " + name)
    dataclass(cls)
    state = cls
    interface = tuple(
        declared_method(name, member)
        for name, member in members.items()
        if not name.startswith("__")
        and isinstance(member, FunctionType)
        and inspect.iscoroutinefunction(member)
    )
    methods = list(interface)
    for base in cls.__mro__[1:]:
        inherited = _DEFINITIONS.get(base)
        if inherited is not None:
            for method in inherited.methods:
                if not any(registered.function is method.function for registered in methods):
                    methods.append(method)
    dispatch = tuple(methods)
    namespace: dict[str, object] = {
        "__init__": Reference.__init__,
        "__getattribute__": Reference.__getattribute__,
        "__setattr__": Reference.__setattr__,
        "__delattr__": Reference.__delattr__,
        "__hash__": object.__hash__,
        "__eq__": object.__eq__,
        "__repr__": object.__repr__,
    }
    for method in interface:
        wrapper = routed(method)
        # Do not install inherited methods on the declared class: doing so would
        # change Python's MRO, especially cooperative super() in diamonds.
        if method.name in declared_names:
            type.__setattr__(state, method.name, wrapper)
        namespace[method.name] = wrapper
    for name in get_type_hints(state):
        if name in {"_actor_route", "_actor_owner", "_actor_snapshot"}:
            raise TypeError("Reserved actor reference field: " + name)
        namespace[name] = StateField(name)
    namespace["__dict__"] = StateField("__dict__")
    proxy = type.__new__(type, cls.__name__ + "Reference", (Reference, state), namespace)
    initializer: Callable[..., None] = state.__init__
    defaults = Defaults.build(state)

    def initialize(instance: object, *args: object, **kwargs: object) -> None:
        object.__setattr__(instance, "_actor_owner", Constructing(get_ident()))
        object.__setattr__(instance, "_actor_snapshot", None)
        try:
            if defaults is not None:
                for name, value in defaults.decode().items():
                    object.__setattr__(instance, name, value)
            initializer(instance, *args, **kwargs)
        finally:
            object.__setattr__(instance, "_actor_owner", None)

    owned_state = type.__new__(
        type,
        cls.__name__ + "State",
        (State, state),
        {
            "__init__": initialize,
            "__getattribute__": State.__getattribute__,
            "__setattr__": State.__setattr__,
            "__delattr__": State.__delattr__,
        },
    )
    _DEFINITIONS[state] = Definition(
        state, Arguments.build(cls.__name__, state, method=False), dispatch, proxy, owned_state
    )
    return state
