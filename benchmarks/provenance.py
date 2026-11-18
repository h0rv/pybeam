import hashlib
import importlib.metadata
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from benchmarks.contract import Source, StrictModel


class Provenance(StrictModel):
    sources: tuple[Source, ...]
    harness_python: str
    harness_executable: str
    harness_executable_sha256: str
    pydantic: str
    psutil: str
    platform: str
    machine: str
    logical_cpus: int | None
    load_average: tuple[float, float, float]
    available_otp_executable: str | None
    available_elixir_executable: str | None
    environment_policy: str


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args], timeout=10)  # noqa: S603,S607


def source(root: Path, *, generated_paths: frozenset[str] = frozenset()) -> Source:
    if not (root / ".git").exists():
        raise ValueError(
            "Benchmark provenance requires Git metadata in the declared source checkout; "
            "source archives without .git are not supported."
        )
    files = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    excluded = {os.fsencode(name) for name in generated_paths}
    if excluded.intersection(git(root, "ls-files", "-z", "--cached").split(b"\0")):
        raise ValueError("Tracked source cannot be excluded as a generated build output")
    digest = hashlib.sha256()
    for name in sorted(set(files.split(b"\0")) - {b""} - excluded):
        path = root / os.fsdecode(name)
        digest.update(name + b"\0")
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(b"missing-or-directory")
    dirty = bool(git(root, "status", "--porcelain"))
    if excluded:
        status = git(root, "status", "--porcelain", "--untracked-files=all", "-z")
        dirty = any(
            entry and entry not in {b"?? " + name for name in excluded}
            for entry in status.split(b"\0")
        )
    return Source(
        path=str(root.resolve()),
        sha=git(root, "rev-parse", "HEAD").decode().strip(),
        dirty=dirty,
        diff_sha256=hashlib.sha256(git(root, "diff", "HEAD", "--binary")).hexdigest(),
        working_tree_sha256=digest.hexdigest(),
    )


def capture(
    python_source: str, native_source: str | None, *, native_snapshot: Source | None = None
) -> Provenance:
    sources = (source(Path(python_source)),)
    if native_source is not None:
        sources += (source(Path(native_source)) if native_snapshot is None else native_snapshot,)
    return Provenance(
        sources=sources,
        harness_python=sys.version,
        harness_executable=sys.executable,
        harness_executable_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        pydantic=importlib.metadata.version("pydantic"),
        psutil=importlib.metadata.version("psutil"),
        platform=platform.platform(),
        machine=platform.machine(),
        logical_cpus=os.cpu_count(),
        load_average=os.getloadavg(),
        available_otp_executable=shutil.which("erl"),
        available_elixir_executable=shutil.which("elixir"),
        environment_policy="Allowlisted PATH/HOME/TMPDIR/LANG/LC_ALL only; values not logged. "
        "Fixed hash seed and single-thread math/OTP settings. No cookies or inherited PYTHONPATH.",
    )
