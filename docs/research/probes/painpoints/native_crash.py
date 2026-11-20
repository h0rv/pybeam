# native_crash.py
import asyncio
import ctypes
import signal
import sys

CLIENTS = 20


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while line := await reader.readline():
        if line == b"parse-upload\n":
            ctypes.string_at(0)
        writer.write(line)
        await writer.drain()


async def serve() -> None:
    server = await asyncio.start_server(handle, "127.0.0.1", 8765)
    print("ready", flush=True)
    async with server:
        await server.serve_forever()


async def user(alive: list[bool], index: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", 8765)
    try:
        for _ in range(40):
            writer.write(b"ping\n")
            await writer.drain()
            if not await reader.readline():
                alive[index] = False
                return
            await asyncio.sleep(0.05)
    except ConnectionError:
        alive[index] = False


async def bad_upload() -> None:
    await asyncio.sleep(0.5)
    reader, writer = await asyncio.open_connection("127.0.0.1", 8765)
    writer.write(b"parse-upload\n")
    await writer.drain()
    await reader.read()


async def drive() -> None:
    server = await asyncio.create_subprocess_exec(
        sys.executable, __file__, "serve", stdout=asyncio.subprocess.PIPE
    )
    try:
        if server.stdout is None or await server.stdout.readline() != b"ready\n":
            raise RuntimeError("Server failed to start")
        alive = [True] * CLIENTS
        async with asyncio.TaskGroup() as group:
            for index in range(CLIENTS):
                group.create_task(user(alive, index))
            group.create_task(bad_upload())
        code = await server.wait()
    finally:
        if server.returncode is None:
            server.kill()
        await server.wait()
    print("server exit:", signal.Signals(-code).name if code < 0 else code)
    print("other users disconnected:", alive.count(False), "of", CLIENTS)


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        asyncio.run(serve())
    else:
        asyncio.run(drive())
