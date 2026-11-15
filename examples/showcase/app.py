import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import RequestResponseEndpoint

from benchmarks.runner import TrialSummary
from examples.showcase.catalog import ROOT, Card, catalog
from examples.showcase.jobs import Job, Jobs, session_directory


class Settings(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    port: Annotated[int, Field(ge=1024, le=65535)] = 38921
    trusted_hornbeam_root: Path | None = None
    trusted_native_root: Path | None = None


@dataclass(frozen=True)
class Measurement:
    job_id: str
    trial: TrialSummary
    backend: str


@dataclass
class Session:
    jobs: Jobs | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = Settings() if settings is None else settings
    session = Session()
    cards = catalog(settings.trusted_hornbeam_root, settings.trusted_native_root)
    templates = Jinja2Templates(directory=ROOT / "examples/showcase")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        temporary = session_directory()
        with temporary as directory:
            async with asyncio.TaskGroup() as group:
                session.jobs = Jobs(group, Path(directory), settings.trusted_native_root)
                try:
                    yield
                finally:
                    await session.jobs.close()
            if not any(job.status == "uncertain" for job in session.jobs.items.values()):
                temporary.cleanup()
            else:
                print("Ownership uncertain; retained local journals: " + directory, flush=True)
            session.jobs = None

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=ROOT / "examples/static"), name="static")
    hosts = {"127.0.0.1:" + str(settings.port), "localhost:" + str(settings.port)}
    origins = {"http://" + host for host in hosts}

    @app.middleware("http")
    async def local_only(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.headers.get("host") not in hosts:
            return PlainTextResponse("Loopback Host required", status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin not in origins:
            return PlainTextResponse("Local Origin required", status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return PlainTextResponse("Cross-site request denied", status_code=403)
        if request.method not in {"GET", "HEAD"}:
            if origin not in origins:
                return PlainTextResponse("Local Origin required for writes", status_code=403)
            if (
                request.url.query
                or request.headers.get("transfer-encoding") is not None
                or (request.headers.get("content-length", "0") != "0")
            ):
                return PlainTextResponse("No command parameters accepted", status_code=400)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def jobs() -> Jobs:
        if session.jobs is None:
            raise HTTPException(503, "Session not started")
        return session.jobs

    def find_job(key: str, manager: Jobs) -> Job:
        job = manager.items.get(key)
        if job is None:
            raise HTTPException(404, "Unknown job")
        return job

    def find_card(key: str) -> Card:
        for card in cards:
            if card.key == key:
                return card
        raise HTTPException(404, "Not an allowed job")

    def render(request: Request, manager: Jobs, *, partial: bool = False) -> HTMLResponse:
        measurements = tuple(
            Measurement(job.id, trial, job.card.title)
            for job in manager.items.values()
            if job.card.key == "smoke" or job.card.benchmark is not None
            for trial in job.trials
        )
        return templates.TemplateResponse(
            request=request,
            name="jobs.html" if partial else "index.html",
            context={
                "cards": cards,
                "jobs": tuple(reversed(manager.items.values())),
                "measurements": measurements,
            },
        )

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, manager: Annotated[Jobs, Depends(jobs)]) -> HTMLResponse:
        return render(request, manager)

    @app.get("/jobs", response_class=HTMLResponse)
    async def job_panel(request: Request, manager: Annotated[Jobs, Depends(jobs)]) -> HTMLResponse:
        return render(request, manager, partial=True)

    @app.post("/start/{key}")
    async def start(key: str, manager: Annotated[Jobs, Depends(jobs)]) -> RedirectResponse:
        try:
            manager.start(find_card(key))
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return RedirectResponse("/#jobs", status_code=303)

    @app.post("/run-all")
    async def run_all(manager: Annotated[Jobs, Depends(jobs)]) -> RedirectResponse:
        available = tuple(
            card for card in cards if card.disabled is None and card.benchmark is not None
        )
        if len(manager.items) + len(available) > 16 or (
            sum(job.active for job in manager.items.values()) + len(available) > 4
        ):
            raise HTTPException(409, "Queue full")
        for card in available:
            manager.start(card)
        return RedirectResponse("/#jobs", status_code=303)

    @app.post("/cancel/{key}")
    async def cancel(key: str, manager: Annotated[Jobs, Depends(jobs)]) -> RedirectResponse:
        manager.stop(find_job(key, manager))
        return RedirectResponse("/#jobs", status_code=303)

    @app.get("/artifact/{key}")
    async def artifact(key: str, manager: Annotated[Jobs, Depends(jobs)]) -> Response:
        job = find_job(key, manager)
        return Response(
            "".join(trial.model_dump_json() + "\n" for trial in job.trials),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=trials.jsonl"},
        )

    @app.get("/source/{key}", response_class=PlainTextResponse)
    async def source(key: str) -> PlainTextResponse:
        card = find_card(key)
        if card.source is None:
            raise HTTPException(404, "Source not in local catalog")
        return PlainTextResponse((ROOT / card.source).read_text())

    return app
