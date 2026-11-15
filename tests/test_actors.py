import subprocess
import sys
from pathlib import Path
from typing import override

import pytest

from pybeam.actors.protocol import ActorSpec, MethodError


class Counter:
    async def add(self, amount: int, /, *, label: str) -> int:
        return amount

    async def read(self) -> int:
        return 0

    @staticmethod
    async def static(amount: int) -> int:
        return amount

    @classmethod
    async def class_method(cls, amount: int) -> int:
        return amount


class Child(Counter):
    pass


class Override(Counter):
    @override
    async def add(self, amount: int, /, *, label: str) -> int:
        return amount + 1


async def external(counter: Counter, amount: int, /, *, label: str) -> int:
    return await counter.add(amount, label=label)


def test_spec__accepts_unbound_async_method() -> None:
    spec = ActorSpec(Counter, (Counter.add,))
    spec.require(Counter.add)
    assert spec.cls is Counter


def test_spec__rejects_compatible_free_function() -> None:
    with pytest.raises(MethodError):
        ActorSpec(Counter, (external,))
    spec = ActorSpec(Counter, (Counter.add,))
    with pytest.raises(MethodError):
        spec.require(external)


def test_spec__rejects_unregistered_member() -> None:
    spec = ActorSpec(Counter, (Counter.add,))
    with pytest.raises(MethodError):
        spec.require(Counter.read)


def test_spec__rejects_bound_method_and_descriptors() -> None:
    for method in (Counter().add, Counter.static, Counter.class_method):
        with pytest.raises(MethodError):
            ActorSpec(Counter, (method,))


def test_spec__respects_inheritance_and_overrides() -> None:
    ActorSpec(Child, (Counter.add,))
    ActorSpec(Override, (Override.add,))
    with pytest.raises(MethodError):
        ActorSpec(Override, (Counter.add,))


def test_spec__rejects_empty_and_duplicate_operations() -> None:
    with pytest.raises(MethodError):
        ActorSpec(Counter, ())
    with pytest.raises(MethodError):
        ActorSpec(Counter, (Counter.add, Counter.add))


def test_spec__does_not_instantiate_actor() -> None:
    class Unconstructable:
        def __init__(self) -> None:
            raise AssertionError("Registration must not instantiate state")

        async def read(self) -> int:
            return 1

    ActorSpec(Unconstructable, (Unconstructable.read,))


def test_spec__imports_without_optional_packages() -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    code = "\n".join(
        (
            "import sys",
            "sys.path.insert(0, {!r})".format(str(source)),
            "from pybeam.actors.protocol import ActorRef, ActorSpec",
            "assert 'hornbeam' not in sys.modules",
            "assert 'erlang' not in sys.modules",
        )
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True, timeout=10)
