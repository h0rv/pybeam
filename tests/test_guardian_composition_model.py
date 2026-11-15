from collections import deque
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class State:
    inner_recorded: bool = False
    outer_recorded: bool = False
    acknowledged: bool = False
    granted: bool = False
    active: bool = True
    inner_alive: bool = True
    outer_alive: bool = True


def successors(state: State, *, composed: bool) -> tuple[State, ...]:
    following = [
        replace(state, active=False),
        replace(state, inner_alive=False),
        replace(state, outer_alive=False),
    ]
    if state.active and state.inner_alive and state.outer_alive:
        following.append(replace(state, inner_recorded=True))
        if state.inner_recorded:
            following.append(replace(state, outer_recorded=True))
        if state.inner_recorded and (state.outer_recorded or not composed):
            following.append(replace(state, acknowledged=True))
        if state.acknowledged:
            following.append(replace(state, granted=True))
    return tuple(following)


def reachable(*, composed: bool) -> frozenset[State]:
    pending = deque([State()])
    visited: set[State] = set()
    while pending:
        state = pending.popleft()
        if state in visited:
            continue
        visited.add(state)
        pending.extend(successors(state, composed=composed))
    return frozenset(visited)


def test_composition__grant_requires_both_durable_journals() -> None:
    states = reachable(composed=True)
    assert any(state.granted for state in states)
    assert all(
        not state.granted or (state.inner_recorded and state.outer_recorded) for state in states
    )
    for state in states:
        if not state.active or not state.inner_alive or not state.outer_alive:
            assert all(child.granted == state.granted for child in successors(state, composed=True))


def test_composition__sampled_discovery_is_not_acknowledged_ownership() -> None:
    assert any(state.granted and not state.outer_recorded for state in reachable(composed=False))
