import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import MISSING, dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from typing import Literal, get_type_hints

from pybeam.codec import Json


class ActorPayloadError(ValueError):
    """A call is not valid data for the declared actor interface."""


class ActorCallError(RuntimeError):
    """An actor method failed. Only its exception name and message cross the boundary."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(kind + ": " + message)


class ActorConstructionError(ActorCallError):
    pass


type FailureCategory = Literal["method", "payload", "construction"]


@dataclass(frozen=True, slots=True)
class Failure:
    category: FailureCategory
    kind: str
    message: str


_FAILURE = Json(Failure)
_OBJECT = Json(dict[str, object])


def encode_failure(error: Exception, category: FailureCategory) -> bytes:
    try:
        message = str(error)[:1024]
    except Exception:
        message = "Exception message is unavailable"
    return b"\x01" + _FAILURE.encode(Failure(category, type(error).__name__, message))


def decode_reply(payload: bytes) -> bytes:
    if payload[:1] == b"\x00":
        return payload[1:]
    if payload == b"\x02":
        raise ActorPayloadError("Encoded actor result exceeds max_bytes")
    if payload[:1] != b"\x01":
        raise RuntimeError("Invalid native actor data response")
    failure = _FAILURE.decode(payload[1:])
    match failure.category:
        case "payload":
            raise ActorPayloadError(failure.kind + ": " + failure.message)
        case "construction":
            raise ActorConstructionError(failure.kind, failure.message)
        case "method":
            raise ActorCallError(failure.kind, failure.message)


CONSTRUCTION_PROBE = b"\xff\xff\xff\xff"


@dataclass(frozen=True, slots=True)
class Defaults:
    """Non-init dataclass defaults, copied through JSON so instances never share them."""

    values: tuple[tuple[str, Json[object], bytes], ...]

    @classmethod
    def build(cls, state: type[object]) -> Defaults | None:
        if not is_dataclass(state):
            raise TypeError("Actor state must be a dataclass")
        hints = get_type_hints(state)
        values: list[tuple[str, Json[object], bytes]] = []
        for item in dataclass_fields(state):
            if item.init or item.default is MISSING:
                continue
            codec: Json[object] = Json(hints[item.name])
            values.append((item.name, codec, codec.encode(item.default)))
        return cls(tuple(values)) if values else None

    def decode(self) -> dict[str, object]:
        return {name: codec.decode(payload) for name, codec, payload in self.values}


@dataclass(frozen=True, slots=True)
class Arguments:
    signature: inspect.Signature
    codecs: tuple[tuple[str, Json[object]], ...]
    constructor: bool
    factories: frozenset[str]

    @classmethod
    def build(cls, name: str, function: Callable[..., object], *, method: bool) -> Arguments:
        signature = inspect.signature(function)
        parameters = list(signature.parameters.values())
        if method:
            parameters = parameters[1:]
        signature = signature.replace(parameters=parameters)
        hints = get_type_hints(function.__init__ if isinstance(function, type) else function)
        constructor = isinstance(function, type)
        factories = (
            {
                item.name
                for item in dataclass_fields(function)
                if item.default_factory is not MISSING
            }
            if isinstance(function, type) and is_dataclass(function)
            else set()
        )
        codecs: list[tuple[str, Json[object]]] = []
        for parameter in parameters:
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                raise TypeError("Actor methods do not support variadic parameters")
            if parameter.name not in hints:
                raise TypeError("Actor parameters require annotations: " + parameter.name)
            codecs.append((parameter.name, Json(hints[parameter.name])))
        # Resource factories run only in native construction. Ordinary data
        # defaults still cross JSON so shallow-frozen defaults cannot
        # accidentally share mutable children between actor instances.
        return cls(
            signature, tuple(codecs), constructor, frozenset(factories) & set(signature.parameters)
        )

    def encode(self, args: tuple[object, ...], kwargs: Mapping[str, object]) -> bytes:
        try:
            bound = self.signature.bind(*args, **kwargs)
            data: dict[str, object] = {}
            for name, codec in self.codecs:
                if name in bound.arguments:
                    data[name] = codec.dump(bound.arguments[name])
                elif name not in self.factories:
                    data[name] = codec.dump(self.signature.parameters[name].default)
            return _OBJECT.encode(data)
        except (TypeError, ValueError) as error:
            raise ActorPayloadError(str(error)) from error

    def decode(self, payload: bytes) -> inspect.BoundArguments:
        try:
            data = _OBJECT.decode(payload)
            unknown = set(data).difference(name for name, _ in self.codecs)
            if unknown:
                raise ActorPayloadError("Unexpected arguments: " + ", ".join(sorted(unknown)))
            values: dict[str, object] = {}
            for name, codec in self.codecs:
                if name in data:
                    values[name] = codec.load(data[name])
                elif not (self.constructor and name in self.factories):
                    raise ActorPayloadError("Missing argument: " + name)
            return inspect.BoundArguments(self.signature, values)
        except (TypeError, ValueError) as error:
            raise ActorPayloadError(str(error)) from error


@dataclass(frozen=True, slots=True)
class Method:
    name: str
    function: Callable[..., Awaitable[object]]
    arguments: Arguments
    result: Json[object]

    @classmethod
    def build(cls, name: str, function: Callable[..., Awaitable[object]]) -> Method:
        hints = get_type_hints(function)
        if "return" not in hints:
            raise TypeError("Actor methods require return annotations: " + name)
        return cls(
            name, function, Arguments.build(name, function, method=True), Json(hints["return"])
        )

    async def dispatch(self, state: object, payload: bytes) -> bytes:
        bound = self.arguments.decode(payload)
        result = await self.function(state, *bound.args, **bound.kwargs)
        try:
            return self.result.encode(result)
        except (TypeError, ValueError) as error:
            raise ActorPayloadError(str(error)) from error

    def decode(self, payload: bytes) -> object:
        return self.result.decode(payload)
