import asyncio
import socket
import sys
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest.mock import patch

import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from examples.showcase.app import Settings, create_app
from examples.showcase.catalog import Card, catalog
from examples.showcase.jobs import Job, Jobs
from tests.showcase_fixtures import controlled_walkthrough

PORT = 38941


def controlled_catalog(
    trusted_hornbeam_root: Path | None = None, trusted_native_root: Path | None = None
) -> tuple[Card, ...]:
    return tuple(
        replace(
            card,
            disabled=None,
            title="Controlled boundary: " + card.title,
            explanation="TEST ONLY. No native processes or actual OTP receipts.",
        )
        if card.key == "distributed"
        else card
        for card in catalog()
    )


class ControlledServer(uvicorn.Server):
    @override
    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        print("READY", flush=True)


async def main() -> None:
    releases: dict[str, asyncio.Event] = {}
    managers: dict[str, Jobs] = {}
    snapshots: dict[str, Job] = {}

    async def execute(self: Jobs, job: Job) -> None:
        async with self.serial:
            current = self.items[job.id]
            if current.status == "stopping":
                self.update(replace(current, status="stopped"))
                return
            managers[job.id] = self
            snapshots[job.id] = current
            release = asyncio.Event()
            releases[job.id] = release
            self.update(replace(current, status="running"))
            try:
                async with asyncio.timeout(20):
                    await release.wait()
            except TimeoutError:
                pass
            current = self.items[job.id]
            if current.status == "stopping":
                self.update(replace(current, status="stopped"))
            else:
                proof = controlled_walkthrough()
                self.update(
                    replace(
                        current, status="finished", observations=proof.observations, proof=proof
                    )
                )
            # Exercise the production stale cancel ownership check after terminal publication.
            self.stop(snapshots[job.id])

    async def finish(request: Request) -> Response:
        for release in releases.values():
            release.set()
        return Response(status_code=204)

    async def observe(request: Request) -> Response:
        for key, manager in managers.items():
            current = manager.items[key]
            if current.active:
                manager.update(
                    replace(current, observations=controlled_walkthrough().observations[:3])
                )
        return Response(status_code=204)

    async def subscriber_count(request: Request) -> Response:
        return JSONResponse(
            {"count": max((len(manager.subscribers) for manager in managers.values()), default=0)}
        )

    async def ready(request: Request) -> Response:
        return Response("Controlled boundary ready")

    with (
        patch("examples.showcase.app.catalog", controlled_catalog),
        patch.object(Jobs, "execute", execute),
    ):
        app = create_app(Settings(port=PORT))
        app.routes.extend(
            [
                Route("/__test__/finish", finish),
                Route("/__test__/observe", observe),
                Route("/__test__/subscribers", subscriber_count),
                Route("/__test__/ready", ready),
            ]
        )
        with socket.socket(fileno=int(sys.argv[1])) as listener:
            await ControlledServer(
                uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
            ).serve(sockets=[listener])


if __name__ == "__main__":
    asyncio.run(main())
