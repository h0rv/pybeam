from typing import assert_type

from docs.research.probes.candidates import ActorRef, Counter, CounterRef


async def generic_call(ref: ActorRef[Counter]) -> int:
    result = await ref.call(Counter.add, 2, label="visits")
    assert_type(result, int)
    return result


async def direct_call(counter: Counter) -> int:
    result = await counter.add(2, label="visits")
    assert_type(result, int)
    return result


async def explicit_call(ref: CounterRef) -> int:
    result = await ref.add(2, label="visits")
    assert_type(result, int)
    return result


def structural_reference(counter: Counter) -> CounterRef:
    return counter


async def external(counter: Counter, amount: int, /, *, label: str) -> int:
    return await counter.add(amount, label=label)


async def nonmember_call(ref: ActorRef[Counter]) -> int:
    result = await ref.call(external, 2, label="visits")
    assert_type(result, int)
    return result
