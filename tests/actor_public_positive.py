from typing import assert_type

from pybeam.actors import ActorRef, ActorSpec, UnboundedMailbox, spawn
from tests.native.actor_public import Counter


async def use_public_actor() -> None:
    spec = ActorSpec(Counter, methods=(Counter.add, Counter.read))
    async with await spawn(spec, mailbox=UnboundedMailbox()) as actor:
        assert_type(await actor.call(Counter.read), int)
        assert_type(await actor.call(Counter.add, 1, label="typed"), tuple[int, object])
        compatible: ActorRef[Counter] = actor
        assert_type(await compatible.call(Counter.read), int)
    await actor.stop()
