# worker_state.py
import asyncio
import multiprocessing
import socket

WORKERS = 4
INCREMENTS = 400
SUBSCRIBERS = 8


def listener() -> socket.socket:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("127.0.0.1", 8766))
    sock.listen(512)
    return sock


async def worker_main() -> None:
    count = 0
    subscribers: list[asyncio.StreamWriter] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal count
        line = await reader.readline()
        if line == b"incr\n":
            count += 1
            writer.write(f"{count}\n".encode())
            await writer.drain()
            writer.close()
        elif line == b"subscribe\n":
            subscribers.append(writer)
            writer.write(b"ok\n")
            await writer.drain()
        elif line == b"publish\n":
            for subscriber in subscribers:
                subscriber.write(b"update\n")
            writer.close()

    server = await asyncio.start_server(handle, sock=listener())
    async with server:
        await server.serve_forever()


def worker() -> None:
    asyncio.run(worker_main())


async def request(line: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", 8766)
    writer.write(line)
    await writer.drain()
    reply = await reader.readline()
    writer.close()
    return reply


async def drive() -> None:
    subscribers = [await asyncio.open_connection("127.0.0.1", 8766) for _ in range(SUBSCRIBERS)]
    for reader, writer in subscribers:
        writer.write(b"subscribe\n")
        await writer.drain()
        await reader.readline()
    replies = [int(await request(b"incr\n")) for _ in range(INCREMENTS)]
    await request(b"publish\n")
    received = 0
    for reader, _ in subscribers:
        try:
            await asyncio.wait_for(reader.readline(), 0.5)
            received += 1
        except TimeoutError:
            pass
    print("increments sent:", INCREMENTS, "highest count any client saw:", max(replies))
    print("subscribers notified:", received, "of", SUBSCRIBERS)


if __name__ == "__main__":
    workers = [multiprocessing.Process(target=worker, daemon=True) for _ in range(WORKERS)]
    for process in workers:
        process.start()
    asyncio.run(asyncio.sleep(0.5))
    asyncio.run(drive())
    for process in workers:
        process.terminate()
        process.join()
