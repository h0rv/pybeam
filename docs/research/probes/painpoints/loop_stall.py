# loop_stall.py
import asyncio
import statistics
import sys
import time

CLIENTS = 20
INTERVAL = 0.005
WINDOW = 4.0


def report_card(rows: int) -> int:
    value = 0
    for index in range(rows):
        value = (value * 31 + index) & 0xFFFFFFFF
    return value


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while line := await reader.readline():
        if line == b"report\n":
            report_card(40_000_000)
        writer.write(line)
        await writer.drain()
    writer.close()


async def client(port: int, latencies: list[float], worst: list[float]) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    end = time.monotonic() + WINDOW
    own = 0.0
    while time.monotonic() < end:
        sent = time.monotonic()
        writer.write(b"ping\n")
        await writer.drain()
        await reader.readline()
        elapsed = (time.monotonic() - sent) * 1000
        latencies.append(elapsed)
        own = max(own, elapsed)
        await asyncio.sleep(INTERVAL)
    writer.close()
    worst.append(own)


async def one_report(port: int) -> None:
    await asyncio.sleep(1.0)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"report\n")
    await writer.drain()
    await reader.readline()
    writer.close()


async def serve() -> None:
    server = await asyncio.start_server(handle, "127.0.0.1", 8767)
    print("ready", flush=True)
    async with server:
        await server.serve_forever()


async def main() -> None:
    server = await asyncio.create_subprocess_exec(
        sys.executable, __file__, "serve", stdout=asyncio.subprocess.PIPE
    )
    try:
        if server.stdout is None or await server.stdout.readline() != b"ready\n":
            raise RuntimeError("Server failed to start")
        port = 8767
        latencies: list[float] = []
        worst: list[float] = []
        async with asyncio.TaskGroup() as group:
            for _ in range(CLIENTS):
                group.create_task(client(port, latencies, worst))
            group.create_task(one_report(port))
    finally:
        if server.returncode is None:
            server.kill()
        await server.wait()
    frozen = sum(1 for value in worst if value > 1000)
    print(
        f"other users: p50 {statistics.median(latencies):.1f} ms, longest wait {max(latencies):.0f} ms"
    )
    print("users frozen for over 1 s:", frozen, "of", CLIENTS)


if __name__ == "__main__":
    asyncio.run(serve() if sys.argv[1:] == ["serve"] else main())
