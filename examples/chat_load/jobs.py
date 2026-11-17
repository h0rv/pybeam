def checksum(size: int) -> int:
    total = 0
    for value in range(size):
        total = (total + value * value) % 1_000_003
    return total


async def checksum_entry(size: int) -> int:
    return checksum(size)
