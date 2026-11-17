import binascii
import json
import math
from base64 import b64decode, b64encode
from collections.abc import Callable, Mapping, Sequence
from dataclasses import MISSING, Field, fields, is_dataclass
from enum import Enum
from pathlib import Path, PurePath
from types import NoneType, UnionType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Literal,
    NewType,
    Protocol,
    TypeAliasType,
    TypeIs,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    override,
)

if TYPE_CHECKING:
    from typing_extensions import TypeForm


class CodecError(ValueError):
    """Data does not match the declared type. The message names the failing location."""


class Codec[T](Protocol):
    """Converts values to bytes and back. Bring your own, such as a Pydantic TypeAdapter."""

    def encode(self, value: T, /) -> bytes: ...

    def decode(self, payload: bytes, /) -> T: ...


class _Invalid(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        self.path: list[str] = []


def _describe(value: object) -> str:
    return type(value).__name__


class _Node:
    def load(self, value: object) -> object:
        raise NotImplementedError

    def dump(self, value: object) -> object:
        raise NotImplementedError


class _Json(_Node):
    @override
    def load(self, value: object) -> object:
        return value

    @override
    def dump(self, value: object) -> object:
        match value:
            case None | bool() | int() | str():
                return value
            case float():
                if not math.isfinite(value):
                    raise _Invalid("expected a finite number")
                return value
            case list() | tuple():
                return [_index(self.dump, item, index) for index, item in enumerate(value)]
            case dict():
                result: dict[str, object] = {}
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise _Invalid("expected string keys, got " + _describe(key))
                    result[key] = _key(self.dump, item, key)
                return result
            case _:
                raise _Invalid("expected JSON data, got " + _describe(value))


class _None(_Node):
    @override
    def load(self, value: object) -> object:
        if value is not None:
            raise _Invalid("expected null, got " + _describe(value))
        return None

    @override
    def dump(self, value: object) -> object:
        return self.load(value)


class _Bool(_Node):
    @override
    def load(self, value: object) -> object:
        if not isinstance(value, bool):
            raise _Invalid("expected bool, got " + _describe(value))
        return value

    @override
    def dump(self, value: object) -> object:
        return self.load(value)


class _Int(_Node):
    @override
    def load(self, value: object) -> object:
        if type(value) is not int:
            raise _Invalid("expected int, got " + _describe(value))
        return value

    @override
    def dump(self, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise _Invalid("expected int, got " + _describe(value))
        return int(value)


class _Float(_Node):
    @override
    def load(self, value: object) -> object:
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise _Invalid("expected float, got " + _describe(value))
        number = float(value)
        if not math.isfinite(number):
            raise _Invalid("expected a finite number")
        return number

    @override
    def dump(self, value: object) -> object:
        return self.load(value)


class _Str(_Node):
    @override
    def load(self, value: object) -> object:
        if type(value) is not str:
            raise _Invalid("expected str, got " + _describe(value))
        return value

    @override
    def dump(self, value: object) -> object:
        if not isinstance(value, str):
            raise _Invalid("expected str, got " + _describe(value))
        return str(value)


class _Bytes(_Node):
    @override
    def load(self, value: object) -> object:
        if not isinstance(value, str):
            raise _Invalid("expected base64 str, got " + _describe(value))
        try:
            return b64decode(value, validate=True)
        except binascii.Error as error:
            raise _Invalid("invalid base64: " + str(error)) from error

    @override
    def dump(self, value: object) -> object:
        if not isinstance(value, bytes):
            raise _Invalid("expected bytes, got " + _describe(value))
        return b64encode(value).decode("ascii")


class _Path(_Node):
    @override
    def load(self, value: object) -> object:
        if not isinstance(value, str):
            raise _Invalid("expected path str, got " + _describe(value))
        return Path(value)

    @override
    def dump(self, value: object) -> object:
        if not isinstance(value, PurePath):
            raise _Invalid("expected Path, got " + _describe(value))
        return str(value)


class _Literal(_Node):
    def __init__(self, values: tuple[object, ...]) -> None:
        for item in values:
            if item is not None and not isinstance(item, bool | int | str):
                raise TypeError("Literal values must be str, int, bool or None")
        self.values = values

    @override
    def load(self, value: object) -> object:
        for item in self.values:
            if type(item) is type(value) and item == value:
                return item
        raise _Invalid("expected one of " + repr(self.values) + ", got " + repr(value))

    @override
    def dump(self, value: object) -> object:
        return self.load(value)


class _Enum(_Node):
    def __init__(self, cls: type[Enum]) -> None:
        self.cls = cls

    @override
    def load(self, value: object) -> object:
        try:
            member = self.cls(value)
        except ValueError as error:
            raise _Invalid("expected a " + self.cls.__name__ + " value") from error
        if type(member.value) is not type(value):
            raise _Invalid("expected a " + self.cls.__name__ + " value, got " + _describe(value))
        return member

    @override
    def dump(self, value: object) -> object:
        if not isinstance(value, self.cls):
            raise _Invalid("expected " + self.cls.__name__ + ", got " + _describe(value))
        return value.value


class _Union(_Node):
    def __init__(self, options: tuple[_Node, ...]) -> None:
        # Floats accept JSON integers, so exact members get the first chance.
        self.options = tuple(item for item in options if not isinstance(item, _Float)) + tuple(
            item for item in options if isinstance(item, _Float)
        )

    def _first(self, convert: Callable[[_Node], object]) -> object:
        failures: list[str] = []
        for option in self.options:
            try:
                return convert(option)
            except _Invalid as error:
                failures.append(error.message)
        raise _Invalid("no union member matched: " + "; ".join(failures))

    @override
    def load(self, value: object) -> object:
        return self._first(lambda option: option.load(value))

    @override
    def dump(self, value: object) -> object:
        return self._first(lambda option: option.dump(value))


def _index(convert: Callable[[object], object], value: object, index: int) -> object:
    try:
        return convert(value)
    except _Invalid as error:
        error.path.append("[" + str(index) + "]")
        raise


def _key(convert: Callable[[object], object], value: object, key: str) -> object:
    try:
        return convert(value)
    except _Invalid as error:
        error.path.append("[" + json.dumps(key) + "]")
        raise


type _Collection = Literal["list", "tuple", "set", "frozenset"]


class _Array(_Node):
    def __init__(self, item: _Node, collection: _Collection, accepts: tuple[type, ...]) -> None:
        self.item = item
        self.collection: _Collection = collection
        self.accepts = accepts

    @override
    def load(self, value: object) -> object:
        if not isinstance(value, list):
            raise _Invalid("expected array, got " + _describe(value))
        items = [_index(self.item.load, item, index) for index, item in enumerate(value)]
        match self.collection:
            case "list":
                return items
            case "tuple":
                return tuple(items)
            case "set":
                return set(_hashable(items))
            case "frozenset":
                return frozenset(_hashable(items))

    @override
    def dump(self, value: object) -> object:
        if not isinstance(value, self.accepts):
            raise _Invalid("expected " + self.collection + ", got " + _describe(value))
        if not isinstance(value, list | tuple | set | frozenset):
            raise _Invalid("expected " + self.collection + ", got " + _describe(value))
        return [_index(self.item.dump, item, index) for index, item in enumerate(value)]


def _hashable(items: list[object]) -> list[object]:
    for index, item in enumerate(items):
        try:
            hash(item)
        except TypeError as error:
            invalid = _Invalid("set members must be hashable, got " + _describe(item))
            invalid.path.append("[" + str(index) + "]")
            raise invalid from error
    return items


class _Tuple(_Node):
    def __init__(self, items: tuple[_Node, ...]) -> None:
        self.items = items

    @override
    def load(self, value: object) -> object:
        if not isinstance(value, list):
            raise _Invalid("expected array, got " + _describe(value))
        if len(value) != len(self.items):
            raise _Invalid("expected " + str(len(self.items)) + " items, got " + str(len(value)))
        return tuple(
            _index(node.load, item, index)
            for index, (node, item) in enumerate(zip(self.items, value, strict=True))
        )

    @override
    def dump(self, value: object) -> object:
        if not isinstance(value, tuple):
            raise _Invalid("expected tuple, got " + _describe(value))
        if len(value) != len(self.items):
            raise _Invalid("expected " + str(len(self.items)) + " items, got " + str(len(value)))
        return [
            _index(node.dump, item, index)
            for index, (node, item) in enumerate(zip(self.items, value, strict=True))
        ]


class _Dict(_Node):
    def __init__(self, key: _Node, item: _Node, accepts: type) -> None:
        if not isinstance(key, _Str | _Int | _Literal | _Enum):
            raise TypeError("Mapping keys must be str, int, a Literal or an Enum")
        self.key = key
        self.item = item
        self.accepts = accepts

    def _load_key(self, key: object) -> object:
        if not isinstance(key, str):
            raise _Invalid("expected string keys, got " + _describe(key))
        if isinstance(self.key, _Int):
            try:
                return int(key)
            except ValueError as error:
                raise _Invalid("expected an integer key, got " + repr(key)) from error
        match self.key:
            case _Literal():
                options: tuple[object, ...] = self.key.values
            case _Enum():
                options = tuple(self.key.cls)
            case _:
                return key
        for option in options:
            try:
                if self._dump_key(option) == key:
                    return option
            except _Invalid:
                continue
        raise _Invalid("unexpected key " + repr(key))

    def _dump_key(self, key: object) -> str:
        dumped = self.key.dump(key)
        if isinstance(dumped, bool) or not isinstance(dumped, int | str):
            raise _Invalid("expected str or int key, got " + _describe(dumped))
        return str(dumped)

    @override
    def load(self, value: object) -> object:
        if not isinstance(value, dict):
            raise _Invalid("expected object, got " + _describe(value))
        result: dict[object, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _Invalid("expected string keys, got " + _describe(key))
            result[_key(self._load_key, key, key)] = _key(self.item.load, item, key)
        return result

    @override
    def dump(self, value: object) -> object:
        if not isinstance(value, self.accepts) or not isinstance(value, Mapping):
            raise _Invalid("expected dict, got " + _describe(value))
        result: dict[str, object] = {}
        for key, item in value.items():
            text = _key(self._dump_key, key, repr(key))
            if not isinstance(text, str):
                raise _Invalid("expected str key")
            result[text] = _key(self.item.dump, item, text)
        return result


class _Dataclass(_Node):
    def __init__(self, cls: type[object], declared: tuple[Field[object], ...]) -> None:
        self.cls = cls
        self.declared = declared
        self.fields: tuple[tuple[str, _Node], ...] = ()
        self.required: frozenset[str] = frozenset()

    def resolve(self, memo: dict[object, _Node]) -> None:
        hints = get_type_hints(self.cls, include_extras=True)
        items = [item for item in self.declared if item.init]
        self.fields = tuple((item.name, _build(hints[item.name], memo)) for item in items)
        self.required = frozenset(
            item.name
            for item in items
            if item.default is MISSING and item.default_factory is MISSING
        )

    @override
    def load(self, value: object) -> object:
        if not isinstance(value, dict):
            raise _Invalid("expected object, got " + _describe(value))
        names = {name for name, _ in self.fields}
        unknown = sorted(str(key) for key in value if key not in names)
        if unknown:
            raise _Invalid("unexpected fields: " + ", ".join(unknown))
        missing = sorted(self.required.difference(value))
        if missing:
            raise _Invalid("missing fields: " + ", ".join(missing))
        arguments: dict[str, object] = {}
        for name, node in self.fields:
            if name in value:
                arguments[name] = _field(node.load, value[name], name)
        try:
            return self.cls(**arguments)
        except (TypeError, ValueError) as error:
            raise _Invalid(self.cls.__name__ + ": " + str(error)) from error

    @override
    def dump(self, value: object) -> object:
        if not isinstance(value, self.cls):
            raise _Invalid("expected " + self.cls.__name__ + ", got " + _describe(value))
        return {name: _field(node.dump, getattr(value, name), name) for name, node in self.fields}


def _field(convert: Callable[[object], object], value: object, name: str) -> object:
    try:
        return convert(value)
    except _Invalid as error:
        error.path.append("." + name)
        raise


class _Alias(_Node):
    def __init__(self) -> None:
        self.target: _Node = _Json()

    @override
    def load(self, value: object) -> object:
        return self.target.load(value)

    @override
    def dump(self, value: object) -> object:
        return self.target.dump(value)


_SCALARS: dict[object, Callable[[], _Node]] = {
    None: _None,
    NoneType: _None,
    bool: _Bool,
    int: _Int,
    float: _Float,
    str: _Str,
    bytes: _Bytes,
    Path: _Path,
    object: _Json,
}


def _build(form: object, memo: dict[object, _Node]) -> _Node:
    try:
        cached = memo.get(form)
    except TypeError:
        cached = None
    if cached is not None:
        return cached
    scalar = _SCALARS.get(form) if isinstance(form, type) or form is None else None
    if scalar is not None:
        return scalar()
    if isinstance(form, TypeAliasType):
        alias = _Alias()
        memo[form] = alias
        alias.target = _build(form.__value__, memo)
        return alias
    if isinstance(form, NewType):
        return _build(form.__supertype__, memo)
    origin = get_origin(form)
    arguments = get_args(form)
    if origin is Annotated:
        return _build(arguments[0], memo)
    if origin is Literal:
        return _Literal(arguments)
    if origin is Union or origin is UnionType:
        return _Union(tuple(_build(item, memo) for item in arguments))
    if isinstance(form, type) and issubclass(form, Enum):
        return _Enum(form)
    if isinstance(form, type) and is_dataclass(form):
        if form.__type_params__:
            raise TypeError("Generic dataclasses are not supported: " + form.__name__)
        node = _Dataclass(form, fields(form))
        memo[form] = node
        node.resolve(memo)
        return node
    container = form if origin is None else origin
    item = _build(arguments[0], memo) if arguments else _Json()
    match container:
        case _ if container is list:
            return _Array(item, "list", (list,))
        case _ if container is Sequence:
            return _Array(item, "list", (list, tuple))
        case _ if container is set:
            return _Array(item, "set", (set,))
        case _ if container is frozenset:
            return _Array(item, "frozenset", (frozenset,))
        case _ if container is tuple:
            if origin is None or (len(arguments) == 2 and arguments[1] is Ellipsis):
                return _Array(item, "tuple", (tuple,))
            return _Tuple(tuple(_build(entry, memo) for entry in arguments))
        case _ if container is dict or container is Mapping:
            key = _build(arguments[0], memo) if arguments else _Str()
            value = _build(arguments[1], memo) if arguments else _Json()
            return _Dict(key, value, dict if container is dict else Mapping)
        case _:
            raise TypeError("Unsupported type for JSON data: " + repr(form))


def _error(error: _Invalid) -> CodecError:
    return CodecError("$" + "".join(reversed(error.path)) + ": " + error.message)


def _reject_constant(value: str) -> object:
    raise CodecError("Non-finite JSON number: " + value)


class Json[T]:
    """Strict JSON for standard types and dataclasses.

    Supports None, bool, int, float, str, bytes (base64), Path, Literal, Enum, unions,
    list, tuple, set, frozenset, dict, Sequence, Mapping, type aliases, NewType and
    non-generic dataclasses. ``object`` means any JSON value. Annotated metadata is
    ignored: express constraints in ``__post_init__``. Unknown and missing dataclass
    fields, non-finite floats and integers for booleans are rejected. Unsupported
    types fail at construction, not on first use.
    """

    __slots__ = ("_node",)

    def __init__(self, form: TypeForm[T]) -> None:
        self._node = _build(form, {})

    def _valid(self, value: object) -> TypeIs[T]:
        try:
            self._node.dump(value)
        except _Invalid:
            return False
        return True

    def dump(self, value: T) -> object:
        """Return JSON-compatible data, checking the value against the declared type."""
        try:
            return self._node.dump(value)
        except _Invalid as error:
            raise _error(error) from None
        except RecursionError as error:
            raise CodecError("Data nests too deeply") from error

    def load(self, data: object) -> T:
        """Parse already-decoded JSON data, such as a TOML or JSON document."""
        try:
            value = self._node.load(data)
        except _Invalid as error:
            raise _error(error) from None
        except RecursionError as error:
            raise CodecError("Data nests too deeply") from error
        if not self._valid(value):
            raise CodecError("$: parsed data does not match the declared type")
        return value

    def encode(self, value: T) -> bytes:
        return json.dumps(
            self.dump(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()

    def decode(self, payload: bytes | str) -> T:
        try:
            data: object = json.loads(payload, parse_constant=_reject_constant)
        except RecursionError as error:
            raise CodecError("JSON nests too deeply") from error
        except CodecError:
            raise
        except ValueError as error:
            raise CodecError("Invalid JSON: " + str(error)) from error
        return self.load(data)
