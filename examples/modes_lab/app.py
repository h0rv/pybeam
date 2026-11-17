import asyncio
import os
import shutil
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TIMEOUT = 240

type Experiment = Literal["fanout", "crash", "fanin"]
type Runtime = Literal["3.14", "3.14t"]
type Engine = Literal["python", "beam"]
type Status = Literal["queued", "running", "done", "failed"]


@dataclass(frozen=True)
class Lane:
    key: str
    label: str
    runtime: Runtime
    engine: Engine
    mode: str
    setting: str
    experiments: tuple[Experiment, ...]


LANES = (
    Lane(
        "asyncio",
        "asyncio",
        "3.14",
        "python",
        "asyncio",
        "asyncio.gather",
        ("fanout", "crash", "fanin"),
    ),
    Lane(
        "threads", "threads", "3.14", "python", "threads", "asyncio.to_thread", ("fanout", "crash")
    ),
    Lane("threads-t", "threads", "3.14t", "python", "threads", "asyncio.to_thread", ("fanout",)),
    Lane(
        "spawn",
        "process per job",
        "3.14",
        "python",
        "spawn",
        "multiprocessing.Process",
        ("fanout", "crash"),
    ),
    Lane(
        "prefork",
        "prefork pool",
        "3.14",
        "python",
        "prefork",
        "ProcessPoolExecutor",
        ("fanout", "crash"),
    ),
    Lane(
        "worker",
        "worker",
        "3.14",
        "beam",
        "worker",
        "ExecutionMode.WORKER",
        ("fanout", "crash", "fanin"),
    ),
    Lane(
        "worker-t", "worker", "3.14t", "beam", "worker", "ExecutionMode.WORKER", ("fanout", "fanin")
    ),
    Lane(
        "owngil", "own GIL", "3.14", "beam", "owngil", "ExecutionMode.OWNGIL", ("fanout", "crash")
    ),
    Lane(
        "isolated",
        "isolated",
        "3.14",
        "beam",
        "isolated",
        "ExecutionMode.ISOLATED",
        ("fanout", "crash"),
    ),
    Lane(
        "isolated-t", "isolated", "3.14t", "beam", "isolated", "ExecutionMode.ISOLATED", ("fanout",)
    ),
    Lane(
        "nodes",
        "separate nodes",
        "3.14",
        "beam",
        "nodes",
        "one BEAM node each",
        ("fanout", "crash"),
    ),
)


class Span(BaseModel):
    start: int
    end: int
    thread: int
    pid: int
    interpreter: int
    gil: bool


class Outcome(BaseModel):
    error: str | None = None
    count: int = 0
    create_us: list[int] = []
    rss_kib: int = 0
    latency_p50_us: int = 0
    latency_p99_us: int = 0
    single_ns: int = 0
    spans: list[Span] = []
    wall_us: int = 0
    others_alive: int | None = None
    victim_result: str = ""
    replacement_us: int = 0
    spawn_us: int = 0
    bytes_per_process: int = 0
    contexts: int = 0
    window: int = 0


@dataclass(frozen=True)
class Bar:
    row: int
    left: float
    width: float
    tone: int


@dataclass
class LaneRun:
    lane: Lane
    status: Status = "queued"
    outcome: Outcome = field(default_factory=Outcome)
    aborted: bool = False
    detail: str = ""

    @property
    def wall_ms(self) -> float:
        return self.outcome.wall_us / 1000

    @property
    def speedup(self) -> float:
        if not self.outcome.wall_us:
            return 0.0
        return len(self.outcome.spans) * self.outcome.single_ns / 1000 / self.outcome.wall_us

    @property
    def create_ms(self) -> float:
        times = self.outcome.create_us
        return sum(times) / len(times) / 1000 if times else 0.0

    @property
    def rss_per_worker(self) -> float:
        return self.outcome.rss_kib / max(self.outcome.count, 1) / 1024

    @property
    def topology(self) -> str:
        spans = self.outcome.spans
        processes = len({span.pid for span in spans})
        threads = len({(span.pid, span.thread) for span in spans})
        interpreters = len({(span.pid, span.interpreter) for span in spans})
        gils = interpreters if any(span.gil for span in spans) else 0
        return "{} OS process{} · {} thread{} · {} interpreter{} · {} GIL{}".format(
            processes,
            "" if processes == 1 else "es",
            threads,
            "" if threads == 1 else "s",
            interpreters,
            "" if interpreters == 1 else "s",
            gils,
            "" if gils == 1 else "s",
        )

    def bars(self, scale_ns: int) -> list[Bar]:
        spans = self.outcome.spans
        if not spans:
            return []
        origin = min(span.start for span in spans)
        tones: dict[tuple[int, int], int] = {}
        bars = []
        for row, span in enumerate(sorted(spans, key=lambda span: span.start)):
            tone = tones.setdefault((span.pid, span.thread), len(tones) % 8)
            bars.append(
                Bar(
                    row,
                    100 * (span.start - origin) / scale_ns,
                    100 * (span.end - span.start) / scale_ns,
                    tone,
                )
            )
        return bars

    @property
    def span_ns(self) -> int:
        spans = self.outcome.spans
        return max(span.end for span in spans) - min(span.start for span in spans) if spans else 0


@dataclass
class Run:
    key: str
    experiment: Experiment
    count: int
    size: int
    lanes: list[LaneRun]

    @property
    def finished(self) -> bool:
        return all(lane.status in ("done", "failed") for lane in self.lanes)

    @property
    def scale_ns(self) -> int:
        return max((lane.span_ns for lane in self.lanes), default=0) or 1


class RunForm(BaseModel):
    experiment: Experiment
    count: Annotated[int, Field(ge=1, le=1_000_000)] = 4
    size: Annotated[int, Field(ge=100_000, le=3_000_000)] = 3_000_000


def native_app(runtime: Runtime) -> Path:
    variable = "PYBEAM_LAB_ERLANG_PYTHON_314T" if runtime == "3.14t" else "PYBEAM_LAB_ERLANG_PYTHON"
    default = ROOT / "_build/hornbeam/checkouts/erlang_python"
    return Path(os.environ.get(variable, default))


def python(runtime: Runtime) -> str:
    if runtime == "3.14t":
        return os.environ.get("PYBEAM_LAB_PYTHON_314T", "python3.14t")
    return sys.executable


def command(lane: Lane, run: Run) -> list[str]:
    arguments = [run.experiment, lane.mode, str(run.count), str(run.size)]
    if lane.engine == "python":
        return [python(lane.runtime), "-m", "examples.modes_lab.baselines", *arguments]
    escript = shutil.which("escript") or "escript"
    return [
        escript,
        str(HERE / "probe.escript"),
        str(native_app(lane.runtime)),
        str(HERE),
        *arguments,
    ]


async def execute(lane: LaneRun, run: Run) -> None:
    lane.status = "running"
    process = await asyncio.create_subprocess_exec(
        *command(lane.lane, run),
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), TIMEOUT)
    except TimeoutError:
        process.kill()
        await process.wait()
        lane.status, lane.detail = "failed", "timed out after {} s".format(TIMEOUT)
        return
    lines = output.decode(errors="replace").strip().splitlines()
    if process.returncode in (-6, 134):
        lane.aborted = True
        lane.status = "done"
        return
    try:
        lane.outcome = Outcome.model_validate_json(lines[-1] if lines else "{}")
    except ValidationError:
        lane.status, lane.detail = "failed", "unreadable probe output"
        return
    lane.status = "done" if lane.outcome.error is None else "failed"
    lane.detail = lane.outcome.error or ""


runs: OrderedDict[str, Run] = OrderedDict()
gate = asyncio.Lock()
tasks: set[asyncio.Task[None]] = set()


async def perform(run: Run) -> None:
    async with gate:
        for lane in run.lanes:
            await execute(lane, run)


templates = Jinja2Templates(directory=HERE)
app = FastAPI(title="PyBeam execution modes")
app.mount("/static", StaticFiles(directory=ROOT / "examples/static"), name="static")
app.mount("/lab", StaticFiles(directory=HERE / "static"), name="lab")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    latest = next(reversed(runs.values()), None)
    return templates.TemplateResponse(
        request, "index.html", {"run": latest, "cores": os.cpu_count()}
    )


@app.post("/runs", response_class=HTMLResponse)
async def start(request: Request, form: Annotated[RunForm, Form()]) -> HTMLResponse:
    count = form.count
    if form.experiment == "fanin":
        count = max(count, 10_000)
    elif count > 8:
        count = 4
    lanes = [LaneRun(lane) for lane in LANES if form.experiment in lane.experiments]
    run = Run(uuid4().hex[:8], form.experiment, count, form.size, lanes)
    runs[run.key] = run
    while len(runs) > 8:
        runs.popitem(last=False)
    task = asyncio.create_task(perform(run))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return templates.TemplateResponse(request, "run.html", {"run": run})


@app.get("/runs/{key}", response_class=HTMLResponse)
async def show(request: Request, key: str) -> HTMLResponse:
    return templates.TemplateResponse(request, "run.html", {"run": runs.get(key)})
