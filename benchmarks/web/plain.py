import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, choices=(1, 2), required=True)
    parser.add_argument("--app", default="benchmarks.web.app:app")
    args = parser.parse_args()
    uvicorn.run(
        str(args.app),
        host="127.0.0.1",
        port=int(os.environ["WEB_PORT"]),
        workers=int(args.workers),
        loop="asyncio",
        http="h11",
        ws="websockets",
        ws_per_message_deflate=False,
        ws_ping_interval=None,
        access_log=False,
        log_level="warning",
        backlog=4096,
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()
