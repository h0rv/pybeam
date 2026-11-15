import shutil
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Card:
    key: str
    title: str
    status: Literal["verified", "experimental", "blocked"]
    explanation: str
    source: str | None
    disabled: str | None
    port: int | None = None
    launcher: Path | None = None


def catalog(trusted_hornbeam_root: Path | None = None) -> tuple[Card, ...]:
    runtime = ROOT / "_build/hornbeam"
    launcher = (
        None
        if trusted_hornbeam_root is None
        else (trusted_hornbeam_root.resolve() / "examples/run_asgi.escript")
    )
    missing: str | None = (
        "No explicitly trusted Hornbeam checkout configured. Native demos disabled."
    )
    if launcher is not None and trusted_hornbeam_root is not None:
        if not launcher.resolve().is_relative_to(trusted_hornbeam_root.resolve()):
            missing = "Trusted launcher escapes its configured source root."
        elif not launcher.is_file() or not runtime.is_dir() or shutil.which("escript") is None:
            missing = (
                "Local built runtime, trusted launcher or escript missing. No automatic build."
            )
        elif find_spec("hornbeam") is None:
            missing = "Hornbeam Python dependency missing in this environment."
        else:
            missing = None
    return (
        Card(
            "smoke",
            "Python benchmark smoke",
            "verified",
            "Run 100 events on four Python queues. Typed completion evidence, not a three-backend "
            "comparison. Uncontrolled host; no comparative performance claims.",
            "benchmarks/python_smoke.py",
            None,
        ),
        Card(
            "hold",
            "Owned cancellation probe",
            "experimental",
            "A small owned Python child waits up to 25 seconds. Stop requests test guardian cleanup. "
            "Cancellation is not rollback; external side effects may remain.",
            "examples/showcase/demo.py",
            None,
        ),
        *(
            Card(
                name,
                title + " counter",
                "verified",
                "Canonical two-browser counter example. UI runs on CPython, counter state on BEAM. "
                "Starts for at most 25 seconds on a private loopback port.",
                "examples/" + name + "/main.py",
                missing
                if missing is not None
                else (
                    None
                    if find_spec(name) is not None
                    else "Optional framework dependency missing."
                ),
                port,
                launcher,
            )
            for name, title, port in (
                ("fastapi", "FastAPI", 38931),
                ("litestar", "Litestar", 38932),
                ("pyview", "PyView", 38933),
            )
        ),
        Card(
            "actors",
            "Native actors (legacy ActorSpec)",
            "experimental",
            "Existing native actor prototype, not the proposed decorator API.",
            "examples/actors/main.py",
            "Managed actor launcher is not integrated with guardian spawn ownership.",
        ),
        Card(
            "scoped",
            "Scoped explicit Protocol references",
            "experimental",
            "Prototype 27d4ded verified explicit native references, not decorators.",
            None,
            "Not in this source catalog. Other worktree heads are never executed.",
        ),
        Card(
            "genstage",
            "GenStage interop",
            "experimental",
            "Prototype 2c06fc: abrupt consumer death leaked a native context. Not unqualified verified.",
            None,
            "Context ownership blocker; no public pipeline feature.",
        ),
        Card(
            "stream",
            "Stream pipeline",
            "experimental",
            "Prototype 0b4dd8 fixed context ownership. Python baseline and native experiment:stream "
            "were checked narrowly, not approved as a public pipeline API.",
            None,
            "Not in this source catalog. No automatic execution from another worktree.",
        ),
        Card(
            "discord",
            "Discord simulation",
            "blocked",
            "Not implemented.",
            None,
            "Requires owned supervision and reference rebinding.",
        ),
        Card(
            "ai",
            "AI event forwarding",
            "experimental",
            "Prototype 8b33c00 forwards Python events. No BEAM runtime transport.",
            None,
            "Not in this source catalog; BEAM integration remains blocked.",
        ),
    )
