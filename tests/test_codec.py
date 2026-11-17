from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Literal

import pytest

from pybeam.codec import CodecError, Json


class Color(StrEnum):
    RED = "red"


class Level(IntEnum):
    LOW = 1


@dataclass(frozen=True, slots=True)
class Point:
    x: int
    y: float = 0.0
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.x < 0:
            raise ValueError("x must not be negative")


@dataclass(frozen=True, slots=True)
class Tree:
    value: int
    children: tuple[Tree, ...] = ()


@dataclass(frozen=True, slots=True)
class Label:
    name: str


type Shape = Point | Literal["origin"]


def test_round_trip__nested_standard_types() -> None:
    codec = Json(dict[Color, list[Point | None]])
    value = {Color.RED: [Point(1, 2.5, ("a",)), None]}
    payload = codec.encode(value)
    assert payload == b'{"red":[{"x":1,"y":2.5,"tags":["a"]},null]}'
    assert codec.decode(payload) == value


def test_round_trip__bytes_paths_enums_and_recursion() -> None:
    assert Json(bytes).decode(Json(bytes).encode(b"\x00\xff")) == b"\x00\xff"
    assert Json(Path).decode(b'"a/b"') == Path("a/b")
    assert Json(Level).decode(b"1") is Level.LOW
    tree = Tree(1, (Tree(2),))
    assert Json(Tree).decode(Json(Tree).encode(tree)) == tree
    assert Json(Shape).decode(b'"origin"') == "origin"
    assert Json(dict[int, str]).decode(b'{"3":"x"}') == {3: "x"}
    assert Json(Annotated[tuple[int, str], "meta"]).decode(b'[1,"a"]') == (1, "a")


def test_float__accepts_integers_and_unions_prefer_exact_types() -> None:
    assert Json(float).decode(b"2") == 2.0
    assert type(Json(float | int).decode(b"2")) is int


@pytest.mark.parametrize(
    ("decode", "payload", "message"),
    [
        (Json(int).decode, b"true", "expected int"),
        (Json(int).decode, b"1.0", "expected int"),
        (Json(bool).decode, b"1", "expected bool"),
        (Json(float).decode, b"NaN", "Non-finite"),
        (Json(float).decode, b"1e400", "finite"),
        (Json(str).decode, b"\xff", "Invalid JSON"),
        (Json(Level).decode, b"true", "Level"),
        (Json(bytes).decode, b'"***"', "base64"),
        (Json(Point).decode, b'{"x":1,"z":2}', "unexpected fields: z"),
        (Json(Point).decode, b"{}", "missing fields: x"),
        (Json(Point).decode, b'{"x":-1}', "x must not be negative"),
        (Json(list[Point]).decode, b'[{"x":1},{"x":"a"}]', r"\$\[1\]\.x: expected int"),
        (Json(tuple[int, int]).decode, b"[1]", "expected 2 items"),
    ],
)
def test_decode__rejects_invalid_data(
    decode: Callable[[bytes], object], payload: bytes, message: str
) -> None:
    with pytest.raises(CodecError, match=message):
        decode(payload)


def test_encode__checks_values_against_the_declared_type() -> None:
    with pytest.raises(CodecError, match=r"\$\.name: expected str"):
        Json(Label).encode(Label(1))  # ty: ignore[invalid-argument-type]
    with pytest.raises(CodecError, match="expected int, got bool"):
        Json(int).encode(True)
    with pytest.raises(CodecError, match="expected list"):
        Json(list[int]).encode((1,))  # ty: ignore[invalid-argument-type]


def test_construction__rejects_unsupported_types() -> None:
    with pytest.raises(TypeError, match="Unsupported"):
        Json(complex)
    with pytest.raises(TypeError, match="keys"):
        Json(dict[float, int])


def test_mapping_keys__integer_enums_and_literals_round_trip() -> None:
    by_level = Json(dict[Level, str])
    assert by_level.decode(by_level.encode({Level.LOW: "x"})) == {Level.LOW: "x"}
    by_literal = Json(dict[Literal[1, "a"], int])
    assert by_literal.decode(by_literal.encode({1: 2, "a": 3})) == {1: 2, "a": 3}
    with pytest.raises(CodecError, match="unexpected key"):
        by_level.decode(b'{"2":"x"}')
    with pytest.raises(TypeError, match="distinct"):
        Json(dict[Literal[1, "1"], int])


def test_union__unhashable_set_members_fall_through_to_later_options() -> None:
    assert Json(set[object] | list[object]).decode(b"[[1]]") == [[1]]
    with pytest.raises(CodecError, match="hashable"):
        Json(set[object]).decode(b"[[1]]")


def test_tuple__empty_shape_rejects_items() -> None:
    assert Json(tuple[()]).decode(b"[]") == ()
    with pytest.raises(CodecError):
        Json(tuple[()]).decode(b"[1]")
