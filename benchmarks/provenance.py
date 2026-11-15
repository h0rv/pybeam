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


def source(root: Path) -> Source:
    files = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    digest = hashlib.sha256()
    for name in sorted(set(files.split(b"\0")) - {b""}):
        path = root / os.fsdecode(name)
        digest.update(name + b"\0")
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(b"missing-or-directory")
    return Source(
        path=str(root.resolve()),
        sha=git(root, "rev-parse", "HEAD").decode().strip(),
        dirty=bool(git(root, "status", "--porcelain")),
        diff_sha256=hashlib.sha256(git(root, "diff", "HEAD", "--binary")).hexdigest(),
        working_tree_sha256=digest.hexdigest(),
    )


def capture(python_source: str, native_source: str) -> Provenance:
    return Provenance(
        sources=(source(Path(python_source)), source(Path(native_source))),
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
