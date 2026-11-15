import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import MISSING, dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from typing import Literal, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model
from typing_extensions import TypeForm


class ActorPayloadError(ValueError):
    """A call is not valid data for the declared actor interface."""


class ActorCallError(RuntimeError):
    """An actor method failed. Only its exception name and message cross the boundary."""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(kind + ": " + message)


class ActorConstructionError(ActorCallError):
    pass


class Failure(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    category: Literal["method", "payload", "construction"]
    kind: str
    message: str


def encode_failure(
    error: Exception, category: Literal["method", "payload", "construction"]
) -> bytes:
    try:
        message = str(error)[:1024]
    except Exception:
        message = "Exception message is unavailable"
    failure = Failure(category=category, kind=type(error).__name__, message=message)
    adapter: TypeAdapter[Failure] = TypeAdapter(Failure)
    return b"\x01" + adapter.dump_json(failure)


def decode_reply(payload: bytes) -> bytes:
    if payload[:1] == b"\x00":
        return payload[1:]
    if payload == b"\x02":
        raise ActorPayloadError("Encoded actor result exceeds max_bytes")
    if payload[:1] != b"\x01":
        raise RuntimeError("Invalid native actor data response")
    failure = Failure.model_validate_json(payload[1:], strict=True)
    match failure.category:
        case "payload":
            raise ActorPayloadError(failure.kind + ": " + failure.message)
        case "construction":
            raise ActorConstructionError(failure.kind, failure.message)
        case "method":
            raise ActorCallError(failure.kind, failure.message)


CONSTRUCTION_PROBE = b"\xff\xff\xff\xff"


_CONFIG = ConfigDict(
    strict=True,
    extra="forbid",
    ser_json_bytes="base64",
    val_json_bytes="base64",
    allow_inf_nan=False,
    validate_default=True,
)


def message_model(name: str, fields: dict[str, tuple[TypeForm[object], object]]) -> type[BaseModel]:
    namespace: dict[str, object] = {
        "__module__": __name__,
        "model_config": _CONFIG,
        "__annotations__": {name: annotation for name, (annotation, _) in fields.items()},
    }
    namespace.update({name: default for name, (_, default) in fields.items()})
    model = type(name, (BaseModel,), namespace)
    return model


@dataclass(frozen=True, slots=True)
class Defaults:
    model: type[BaseModel]
    payload: bytes
    names: tuple[str, ...]

    @classmethod
    def build(cls, state: type[object]) -> Defaults | None:
        if not is_dataclass(state):
            raise TypeError("Actor state must be a dataclass")
        hints = get_type_hints(state)
        names: list[str] = []
        fields: dict[str, tuple[TypeForm[object], object]] = {}
        values: dict[str, object] = {}
        for item in dataclass_fields(state):
            if item.init or item.default is MISSING:
                continue
            internal = "default_" + str(len(names))
            names.append(item.name)
            fields[internal] = (hints[item.name], ...)
            values[internal] = item.default
        if not names:
            return None
        model = message_model(state.__name__ + "Defaults", fields)
        validated = model.model_validate(values, strict=True)
        adapter: TypeAdapter[BaseModel] = TypeAdapter(model)
        return cls(model, adapter.dump_json(validated, warnings="error"), tuple(names))

    def decode(self) -> dict[str, object]:
        model = self.model.model_validate_json(self.payload, strict=True)
        return {
            name: object.__getattribute__(model, "default_" + str(index))
            for index, name in enumerate(self.names)
        }


@dataclass(frozen=True, slots=True)
class Arguments:
    signature: inspect.Signature
    model: type[BaseModel]
    constructor: bool
    factory_fields: frozenset[str]

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
        factory_fields: set[str] = set()
        fields: dict[str, tuple[TypeForm[object], object]] = {}
        for index, parameter in enumerate(parameters):
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                raise TypeError("Actor methods do not support variadic parameters")
            if parameter.name not in hints:
                raise TypeError("Actor parameters require annotations: " + parameter.name)
            default = ... if parameter.default is parameter.empty else parameter.default
            internal = "argument_" + str(index)
            factory = constructor and parameter.name in factories
            if factory:
                # Resource factories run only in native construction. Ordinary
                # data defaults still cross JSON so shallow-frozen defaults cannot
                # accidentally share mutable children between actor instances.
                default = None
                factory_fields.add(internal)
            fields[internal] = (
                hints[parameter.name],
                Field(default=default, alias=parameter.name, validate_default=not factory),
            )
        model = message_model(name, fields)
        model.model_json_schema()
        return cls(signature, model, constructor, frozenset(factory_fields))

    def encode(self, args: tuple[object, ...], kwargs: Mapping[str, object]) -> bytes:
        try:
            bound = self.signature.bind(*args, **kwargs)
            model = self.model.model_validate(bound.arguments, strict=True)
            adapter: TypeAdapter[BaseModel] = TypeAdapter(self.model)
            return adapter.dump_json(
                model,
                warnings="error",
                by_alias=True,
                exclude=set(self.factory_fields).difference(model.model_fields_set),
            )
        except (TypeError, ValueError) as error:
            raise ActorPayloadError(str(error)) from error

    def decode(self, payload: bytes) -> inspect.BoundArguments:
        try:
            model = self.model.model_validate_json(payload, strict=True)
            # Read declared fields directly: model_dump would erase nested model types.
            values = {
                name: object.__getattribute__(model, "argument_" + str(index))
                for index, name in enumerate(self.signature.parameters)
                if not self.constructor or "argument_" + str(index) in model.model_fields_set
            }
            return inspect.BoundArguments(self.signature, values)
        except (TypeError, ValueError) as error:
            raise ActorPayloadError(str(error)) from error


@dataclass(frozen=True, slots=True)
class Method:
    name: str
    function: Callable[..., Awaitable[object]]
    arguments: Arguments
    result: type[BaseModel]

    @classmethod
    def build(cls, name: str, function: Callable[..., Awaitable[object]]) -> Method:
        hints = get_type_hints(function)
        if "return" not in hints:
            raise TypeError("Actor methods require return annotations: " + name)
        result = create_model(name + "Return", __config__=_CONFIG, value=(hints["return"], ...))
        result.model_json_schema()
        return cls(name, function, Arguments.build(name, function, method=True), result)

    async def dispatch(self, state: object, payload: bytes) -> bytes:
        bound = self.arguments.decode(payload)
        result = await self.function(state, *bound.args, **bound.kwargs)
        try:
            value = self.result.model_validate({"value": result}, strict=True)
            adapter: TypeAdapter[BaseModel] = TypeAdapter(self.result)
            return adapter.dump_json(value, warnings="error")
        except (TypeError, ValueError) as error:
            raise ActorPayloadError(str(error)) from error

    def decode(self, payload: bytes) -> object:
        model = self.result.model_validate_json(payload, strict=True)
        return object.__getattribute__(model, "value")
