import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from benchmarks.runner import TrialSummary
from examples.showcase.catalog import ROOT, catalog
from examples.showcase.jobs import Job, Jobs, session_directory
from pybeam.codec import Json


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    port: int = 38921
    trusted_hornbeam_root: Path | None = None
    trusted_native_root: Path | None = None

    def __post_init__(self) -> None:
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("port must be an integer from 1024 to 65535")


@dataclass(frozen=True)
class Measurement:
    job_id: str
    trial: TrialSummary
    backend: str


@dataclass
class Session:
    jobs: Jobs | None = None


def create_app(settings: Settings | None = None) -> Starlette:
    settings = Settings() if settings is None else settings
    session = Session()
    cards = catalog(settings.trusted_hornbeam_root, settings.trusted_native_root)
    templates = Jinja2Templates(directory=ROOT / "examples/showcase")

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncGenerator[None]:
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

    def jobs() -> Jobs:
        if session.jobs is None:
            raise HTTPException(503, "Session not started")
        return session.jobs

    def key(request: Request) -> str:
        value: object = request.path_params["key"]
        if not isinstance(value, str):
            raise HTTPException(400, "Invalid job key")
        return value

    def find_job(request: Request) -> Job:
        job = jobs().items.get(key(request))
        if job is None:
            raise HTTPException(404, "Unknown job")
        return job

    def render(request: Request, *, partial: bool = False) -> HTMLResponse:
        manager = jobs()
        selected = request.query_params.get("scenario", "distributed")
        card = next((item for item in cards if item.key == selected), None)
        if card is None:
            raise HTTPException(404, "Unknown scenario")
        scenario_jobs = tuple(
            job for job in reversed(manager.items.values()) if job.card.key == selected
        )
        measurements = tuple(
            Measurement(job.id, trial, job.card.title)
            for job in scenario_jobs
            if job.card.key == "smoke" or job.card.benchmark is not None
            for trial in job.trials
        )
        return templates.TemplateResponse(
            request=request,
            name="jobs.html" if partial else "index.html",
            context={
                "cards": cards,
                "card": card,
                "jobs": scenario_jobs,
                "measurements": measurements,
                "revision": manager.revision,
            },
        )

    def acknowledgment(request: Request, scenario: str) -> Response:
        if request.headers.get("hx-request") == "true":
            # An enhanced POST never returns a snapshot that could overwrite a later SSE read.
            return Response(status_code=204)
        return RedirectResponse("/?scenario=" + scenario, status_code=303)

    async def favicon(request: Request) -> Response:
        return Response(status_code=204)

    async def index(request: Request) -> Response:
        if "scenario" not in request.query_params:
            # The supervised chat is the primary story; the scripted walkthrough is secondary.
            return RedirectResponse("/chat", status_code=303)
        return render(request)

    async def chat(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="chat.html")

    async def job_panel(request: Request) -> HTMLResponse:
        return render(request, partial=True)

    async def events(request: Request) -> Response:
        manager = jobs()
        try:
            queue = manager.subscribe()
        except ValueError as error:
            raise HTTPException(503, str(error)) from error

        async def stream() -> AsyncGenerator[str]:
            try:
                while not manager.closing:
                    try:
                        async with asyncio.timeout(15):
                            revision = await queue.get()
                        yield "event: jobs-changed\nid: " + str(revision) + "\ndata: changed\n\n"
                    except TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                manager.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def start(request: Request) -> Response:
        card = next((card for card in cards if card.key == key(request)), None)
        if card is None:
            raise HTTPException(404, "Not an allowed job")
        try:
            jobs().start(card)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return acknowledgment(request, card.key)

    async def run_all(request: Request) -> Response:
        manager = jobs()
        available = tuple(
            card for card in cards if card.disabled is None and card.benchmark is not None
        )
        if len(manager.items) + len(available) > 16 or (
            sum(job.active for job in manager.items.values()) + len(available) > 4
        ):
            raise HTTPException(409, "Queue full")
        for card in available:
            manager.start(card)
        return acknowledgment(request, "benchmark-python")

    async def cancel(request: Request) -> Response:
        job = find_job(request)
        jobs().stop(job)
        return acknowledgment(request, job.card.key)

    async def artifact(request: Request) -> Response:
        job = find_job(request)
        if job.card.key == "distributed":
            if job.proof is None:
                raise HTTPException(409, "No completed sanitized walkthrough proof")
            return Response(
                Json(type(job.proof)).encode(job.proof).decode(),
                media_type="application/json",
                headers={
                    "Content-Disposition": "attachment; filename=walkthrough-" + job.id + ".json"
                },
            )
        return Response(
            "".join(Json(type(trial)).encode(trial).decode() + "\n" for trial in job.trials),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=trials.jsonl"},
        )

    async def source(request: Request) -> PlainTextResponse:
        card = next((card for card in cards if card.key == key(request)), None)
        if card is None or card.source is None:
            raise HTTPException(404, "Source not in local catalog")
        return PlainTextResponse((ROOT / card.source).read_text())

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/favicon.ico", favicon),
            Route("/", index),
            Route("/chat", chat, methods=["GET"]),
            Route("/jobs", job_panel),
            Route("/events", events),
            Route("/start/{key}", start, methods=["POST"]),
            Route("/run-all", run_all, methods=["POST"]),
            Route("/cancel/{key}", cancel, methods=["POST"]),
            Route("/artifact/{key}", artifact),
            Route("/source/{key}", source),
            Mount("/static", StaticFiles(directory=ROOT / "examples/static"), name="static"),
        ],
    )
    hosts = {"127.0.0.1:" + str(settings.port), "localhost:" + str(settings.port)}
    origins = {"http://" + host for host in hosts}

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
        if request.url.path == "/chat":
            # Only this page can frame the separately launched local chat.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' "
                "'sha256-i/5yytnSVuElZsMlotX8mpio4Fpgs88VEXd1xgxRHbI='; "
                "frame-src http://127.0.0.1:8766; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=local_only)
    return app
