from collections import deque
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class State:
    inner_recorded: bool = False
    outer_recorded: bool = False
    acknowledged: bool = False
    granted: bool = False
    owner_alive: bool = True
    control_alive: bool = True
    cancelled: bool = False
    inner_alive: bool = True
    outer_alive: bool = True

    @property
    def can_authorize(self) -> bool:
        return (
            self.owner_alive
            and self.control_alive
            and not self.cancelled
            and self.inner_alive
            and self.outer_alive
        )


def successors(state: State, *, composed: bool) -> tuple[State, ...]:
    following = [
        replace(state, owner_alive=False),
        replace(state, control_alive=False),
        replace(state, cancelled=True),
        replace(state, inner_alive=False),
        replace(state, outer_alive=False),
    ]
    if state.can_authorize:
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
        if not state.can_authorize:
            assert all(child.granted == state.granted for child in successors(state, composed=True))


def test_composition__sampled_discovery_is_not_acknowledged_ownership() -> None:
    assert any(state.granted and not state.outer_recorded for state in reachable(composed=False))
