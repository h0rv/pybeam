from docs.research.probes.candidates import Counter, Other


async def wrong_receiver(other: Other) -> int:
    return await Counter.add(other, 2, label="visits")


async def wrong_positional(counter: Counter) -> int:
    return await counter.add("two", label="visits")


async def wrong_keyword_value(counter: Counter) -> int:
    return await counter.add(2, label=3)


async def wrong_keyword_name(counter: Counter) -> int:
    return await counter.add(2, lable="visits")


async def missing_keyword(counter: Counter) -> int:
    return await counter.add(2)


async def positional_only(counter: Counter) -> int:
    return await counter.add(amount=2, label="visits")


async def wrong_return(counter: Counter) -> str:
    return await counter.add(2, label="visits")
