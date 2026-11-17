from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest

import pybeam
from pybeam.actors import ExecutionLimits, groups
from pybeam.codec import Json


def construct_group(
    constructor: Callable[..., pybeam.ActorGroup], options: dict[str, object]
) -> pybeam.ActorGroup:
    return constructor(**options)


@pytest.mark.parametrize("limit", ["max_children", "max_pending", "max_bytes"])
@pytest.mark.parametrize(
    "invalid",
    [
        True,
        False,
        "1",
        "",
        0,
        -1,
        1.0,
        1.5,
        0.0,
        -1.0,
        float("inf"),
        float("-inf"),
        float("nan"),
        None,
    ],
)
def test_actor_group_limits__reject_invalid_values_before_native_allocation(
    limit: str, invalid: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    allocations: list[int] = []

    async def open_group(*, max_children: int) -> groups.DataGroup:
        allocations.append(max_children)
        pytest.fail("Invalid limits must not allocate a native group")

    monkeypatch.setattr(groups, "open_group", open_group)
    # Exercise the runtime object boundary without weakening the public signature.
    with pytest.raises(ValueError) as raised:
        construct_group(pybeam.ActorGroup, {limit: invalid})
    assert limit in str(raised.value)
    assert allocations == []


def test_actor_group_limits__preserve_defaults_and_positive_integer_kwargs() -> None:
    defaults = pybeam.ActorGroup()._limits
    assert (defaults.max_children, defaults.max_pending, defaults.max_bytes) == (
        128,
        64,
        1_048_576,
    )
    configured = pybeam.ActorGroup(max_children=1, max_pending=2, max_bytes=3)._limits
    assert (configured.max_children, configured.max_pending, configured.max_bytes) == (1, 2, 3)


@pytest.mark.parametrize("limit", ["workers", "capacity", "max_bytes"])
@pytest.mark.parametrize("invalid", [True, False, 0, -1, "1", 1.0, float("inf"), float("nan")])
def test_execution_limits__reject_invalid_values(limit: str, invalid: object) -> None:
    options: dict[str, object] = {"workers": 4, "capacity": 32, "max_bytes": 1_048_576}
    options[limit] = invalid
    with pytest.raises(ValueError) as raised:
        Json(ExecutionLimits).load(options)
    assert limit in str(raised.value)


def test_execution_limits__immutable_strict_value_and_public_exports() -> None:
    limits = pybeam.ExecutionLimits(workers=4, capacity=32, max_bytes=1_048_576)
    assert type(limits) is ExecutionLimits
    assert Json(type(limits)).dump(limits) == {"workers": 4, "capacity": 32, "max_bytes": 1_048_576}
    with pytest.raises(FrozenInstanceError):
        limits.__setattr__("workers", 8)
    with pytest.raises(ValueError, match="unexpected fields"):
        Json(ExecutionLimits).load({**Json(type(limits)).dump(limits), "unexpected": 1})
