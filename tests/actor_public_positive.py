from typing import assert_type

from pybeam.actors.native import spawn
from pybeam.actors.protocol import ActorRef, ActorSpec
from pybeam.mailbox import UnboundedMailbox
from tests.native.actor_public import Counter


async def use_public_actor() -> None:
    spec = ActorSpec(Counter, methods=(Counter.add, Counter.read))
    async with await spawn(spec, mailbox=UnboundedMailbox()) as actor:
        assert_type(await actor.call(Counter.read), int)
        assert_type(await actor.call(Counter.add, 1, label="typed"), tuple[int, object])
        compatible: ActorRef[Counter] = actor
        assert_type(await compatible.call(Counter.read), int)
    await actor.stop()
