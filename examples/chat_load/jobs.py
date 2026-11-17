def checksum(size: int) -> int:
    total = 0
    for value in range(size):
        total = (total + value * value) % 1_000_003
    return total


async def checksum_entry(payload: bytes) -> bytes:
    return str(checksum(int(payload))).encode()
