from docs.research.probes.candidates import CounterRef, Other


async def wrong_receiver(other: Other) -> CounterRef:
    return other


async def wrong_positional(ref: CounterRef) -> int:
    return await ref.add("two", label="visits")


async def wrong_keyword_value(ref: CounterRef) -> int:
    return await ref.add(2, label=3)


async def wrong_keyword_name(ref: CounterRef) -> int:
    return await ref.add(2, lable="visits")


async def missing_keyword(ref: CounterRef) -> int:
    return await ref.add(2)


async def positional_only(ref: CounterRef) -> int:
    return await ref.add(amount=2, label="visits")


async def wrong_return(ref: CounterRef) -> str:
    return await ref.add(2, label="visits")
