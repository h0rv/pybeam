import argparse
import asyncio
import os

from pybeam.integrations.hornbeam import run
from uvicorn import Config, Server


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default="benchmarks.web.app:app")
    args = parser.parse_args()
    server = Server(
        Config(
            str(args.app),
            host="127.0.0.1",
            port=int(os.environ["WEB_PORT"]),
            loop="none",
            http="h11",
            ws="websockets",
            ws_per_message_deflate=False,
            ws_ping_interval=None,
            access_log=False,
            log_level="warning",
            timeout_graceful_shutdown=3,
            backlog=4096,
        )
    )
    try:
        await server.serve()
    except asyncio.CancelledError:
        if server.started:
            await server.shutdown()
        raise


if __name__ == "__main__":
    run(main)
