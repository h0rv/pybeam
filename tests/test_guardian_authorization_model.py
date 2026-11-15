from collections.abc import Iterator
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import assert_never


class Life(Enum):
    LIVE = auto()
    GONE = auto()
    REUSED_PID = auto()


class Phase(Enum):
    WAITING = auto()
    REGISTERED = auto()
    ACKED = auto()
    GUARDIAN_CHECKED = auto()
    GRANTED = auto()
    ENTERED = auto()
    STOPPED = auto()


class Binding(Enum):
    MATCH = auto()
    WRONG_OWNER = auto()
    WRONG_GUARDIAN = auto()
    WRONG_GENERATION = auto()
    WRONG_PROCESS = auto()


@dataclass(frozen=True, slots=True)
class Grant:
    owner_observation: Life
    guardian_observation: Life
    binding: Binding


@dataclass(frozen=True, slots=True)
class State:
    owner: Life = Life.LIVE
    guardian: Life = Life.LIVE
    phase: Phase = Phase.WAITING
    binding: Binding = Binding.MATCH
    grant: Grant | None = None


def transitions(state: State, *, unsafe: bool) -> Iterator[tuple[str, State]]:
    if state.owner is Life.LIVE:
        yield "owner_dies", replace(state, owner=Life.GONE)
    if state.owner is Life.GONE:
        yield "owner_pid_reused", replace(state, owner=Life.REUSED_PID)
    if state.guardian is Life.LIVE:
        yield "guardian_dies", replace(state, guardian=Life.GONE)
    if state.guardian is Life.GONE:
        yield "guardian_pid_reused", replace(state, guardian=Life.REUSED_PID)
    if state.phase is not Phase.STOPPED:
        yield "teardown", replace(state, phase=Phase.STOPPED)
    match state.phase:
        case Phase.WAITING:
            if state.owner is Life.LIVE and state.guardian is Life.LIVE:
                yield "register", replace(state, phase=Phase.REGISTERED)
        case Phase.REGISTERED:
            if state.guardian is Life.LIVE and (unsafe or state.owner is Life.LIVE):
                yield "ack_owner_observation", replace(state, phase=Phase.ACKED)
        case Phase.ACKED:
            for binding in Binding:
                if binding is not Binding.MATCH and state.binding is Binding.MATCH:
                    yield binding.name, replace(state, binding=binding)
            if state.guardian is Life.LIVE and state.binding is Binding.MATCH:
                yield "gate_guardian_observation", replace(state, phase=Phase.GUARDIAN_CHECKED)
        case Phase.GUARDIAN_CHECKED:
            if unsafe or state.owner is Life.LIVE:
                yield (
                    "final_owner_observation_and_grant",
                    replace(
                        state,
                        phase=Phase.GRANTED,
                        grant=Grant(state.owner, Life.LIVE, state.binding),
                    ),
                )
        case Phase.GRANTED:
            yield "entry", replace(state, phase=Phase.ENTERED)
        case Phase.ENTERED | Phase.STOPPED:
            pass
        case _ as unexpected:
            assert_never(unexpected)


def traces(*, unsafe: bool) -> dict[State, tuple[str, ...]]:
    initial = State()
    seen: dict[State, tuple[str, ...]] = {initial: ()}
    frontier = {initial}
    for _ in range(10):
        following: set[State] = set()
        for state in frontier:
            for event, result in transitions(state, unsafe=unsafe):
                if result not in seen:
                    seen[result] = (*seen[state], event)
                    following.add(result)
        frontier = following
    return seen


def test_authorization_model__fresh_owner_observation_is_required_at_execution_grant() -> None:
    # Grant is the final owner liveness observation, not execv or the first instruction.
    # Guardian death may race after its separate successful observation. Cleanup is
    # still required; no atomic multi-process OS liveness transaction is assumed.
    states = traces(unsafe=False)
    assert len(states) > 100
    for state, trace in states.items():
        if state.phase in {Phase.GRANTED, Phase.ENTERED}:
            assert state.grant == Grant(Life.LIVE, Life.LIVE, Binding.MATCH), trace
    assert any(state.phase is Phase.ENTERED for state in states)
    assert any(state.phase is Phase.ENTERED and state.owner is Life.GONE for state in states)
    assert any(state.phase is Phase.STOPPED and state.owner is Life.REUSED_PID for state in states)


def test_authorization_model__original_protocol_allows_dead_owner_entry() -> None:
    states = traces(unsafe=True)
    counterexamples = [
        trace
        for state, trace in states.items()
        if state.phase is Phase.ENTERED
        and state.grant is not None
        and state.grant.owner_observation is not Life.LIVE
    ]
    assert counterexamples
    assert any(
        trace.index("owner_dies") < trace.index("final_owner_observation_and_grant")
        for trace in counterexamples
    )
