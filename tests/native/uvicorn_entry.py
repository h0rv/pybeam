import asyncio
import sys

from uvicorn import Config, Server

from pybeam.integrations.hornbeam import run


async def main() -> None:
    config = Config(sys.argv[1], host="127.0.0.1", port=int(sys.argv[2]), loop="none", http="h11")
    server = Server(config)
    try:
        await server.serve()
    except asyncio.CancelledError:
        if server.started:
            await server.shutdown()
        raise


if __name__ == "__main__":
    run(main)
