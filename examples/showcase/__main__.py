import argparse
from pathlib import Path

import uvicorn

from examples.showcase.app import Settings, create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local CPython control plane. Run from the checkout: "
        "uv run --extra showcase python -m examples.showcase. "
        "Open http://127.0.0.1:38921. Fixed local commands only; no public bind option. "
        "One serial job, four queued/running, 30-second guardian budget, 16 retained jobs. "
        "Ctrl+C requests owned cleanup. Clean artifacts are removed on exit. "
        "Uncertain ownership journals are retained and block new measurements, even after restart. "
        "Three scalar reference profiles, not comparative performance evidence."
    )
    parser.add_argument(
        "--port", type=int, default=38921, help="Private loopback port (default 38921)"
    )
    parser.add_argument(
        "--trusted-hornbeam-root",
        type=Path,
        help="Explicitly trust a local Hornbeam checkout's examples/run_asgi.escript. "
        "Expected contract: loopback bind and ASGI module argv. Uses this project's _build/hornbeam "
        "and installed framework dependencies. Never builds, fetches or executes other worktree heads "
        "without this option. Counter sessions use ports 38931-38933, never 8642.",
    )
    parser.add_argument(
        "--trusted-native-root",
        type=Path,
        help="Explicit private a1ca04b native checkout with verified benchmark build manifest. "
        "No build or source changes are performed by the website.",
    )
    args = parser.parse_args()
    settings = Settings(
        port=args.port,
        trusted_hornbeam_root=args.trusted_hornbeam_root,
        trusted_native_root=args.trusted_native_root,
    )
    # Uvicorn drains HTTP before lifespan shutdown. Bound the SSE drain so lifespan
    # can request owned cleanup, then let its TaskGroup await the guardian ACKs.
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=settings.port,
        access_log=False,
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()
