from pybeam.codec import Json

INTEGER = Json(int)


async def iterative_sum(payload: bytes) -> bytes:
    remaining = INTEGER.decode(payload)
    total = 0
    while remaining != 0:
        total += remaining
        remaining -= 1
    return INTEGER.encode(total)


async def formula_sum(payload: bytes) -> bytes:
    upto = INTEGER.decode(payload)
    if upto < 0:
        raise ValueError("The upper bound must be nonnegative")
    return INTEGER.encode(upto * (upto + 1) // 2)
