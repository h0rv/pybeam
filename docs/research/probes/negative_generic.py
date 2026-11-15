from docs.research.probes.candidates import ActorRef, Counter, Other


async def wrong_receiver(ref: ActorRef[Counter]) -> bytes:
    return await ref.call(Other.read, b"key")


async def wrong_positional(ref: ActorRef[Counter]) -> int:
    return await ref.call(Counter.add, "two", label="visits")


async def wrong_keyword_value(ref: ActorRef[Counter]) -> int:
    return await ref.call(Counter.add, 2, label=3)


async def wrong_keyword_name(ref: ActorRef[Counter]) -> int:
    return await ref.call(Counter.add, 2, lable="visits")


async def missing_keyword(ref: ActorRef[Counter]) -> int:
    return await ref.call(Counter.add, 2)


async def positional_only(ref: ActorRef[Counter]) -> int:
    return await ref.call(Counter.add, amount=2, label="visits")


async def wrong_return(ref: ActorRef[Counter]) -> str:
    return await ref.call(Counter.add, 2, label="visits")


async def bound_method(ref: ActorRef[Counter], counter: Counter) -> int:
    return await ref.call(counter.add, 2, label="visits")
