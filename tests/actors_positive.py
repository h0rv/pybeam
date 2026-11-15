from typing import assert_type

from pybeam.actors.protocol import ActorRef, ActorSpec
from tests.test_actors import Counter, external


async def calls(ref: ActorRef[Counter]) -> int:
    spec = ActorSpec(Counter, (Counter.add,))
    assert_type(spec, ActorSpec[Counter])
    result = await ref.call(Counter.add, 2, label="visits")
    assert_type(result, int)
    return result


async def compatible_nonmember(ref: ActorRef[Counter]) -> int:
    result = await ref.call(external, 2, label="visits")
    assert_type(result, int)
    return result
