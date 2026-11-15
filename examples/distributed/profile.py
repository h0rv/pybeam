import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from pybeam.runtime.assets import packaged_assets
from pybeam.runtime.optional_manifest import contained, verify_manifest

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "build/distribution-ready-profile.json"
PROFILE_SHA256 = "b46c8cce60981807a15823901da2a2c4c50b089380fc905c19382da49c56f7f7"


class PreparedProfile(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    sdk_base: Literal["0766a38b4b2523e439ce5de3e755932de2cc36de"]
    native_head: Literal["d227c0de29fa3f748610352bf8a808a235bcd5c3"]
    native_c_tree: Literal["5af0fa2d8286302d153a386947587802e7ff0465"]
    native_source: Path
    raw_nif_sha256: Literal["07d3af12b9afa88cedfdc0aea0dac3afe029e42f14c5bb84c20020cd9a5b0e41"]
    staged_nif_sha256: Literal["75d042c0aa9849f20216493313f3a5020250746016e413b11f2bc2f8590ef121"]
    sdk_files: dict[str, str]
    native_files: dict[str, str]
    runtime_files: dict[str, str]
    origin_receipt_sha256: str
    compiler_jobs: Literal[0]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_files(root: Path, files: dict[str, str]) -> None:
    for name, expected in files.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Prepared profile path escaped its root")
        if digest(contained(root, relative)) != expected:
            raise ValueError("Prepared profile source or asset bytes differ")


def verify_prepared() -> None:
    if digest(PROFILE) != PROFILE_SHA256:
        raise ValueError("Prepared distribution profile pin differs")
    profile = PreparedProfile.model_validate_json(PROFILE.read_bytes())
    native = profile.native_source
    git = shutil.which("git")
    if git is None:
        raise ValueError("Source profile verification requires git")
    for reference, expected in (
        ("HEAD", profile.native_head),
        ("HEAD:c_src", profile.native_c_tree),
    ):
        actual = subprocess.check_output(  # noqa: S603
            [git, "-C", str(native), "rev-parse", reference], text=True, timeout=5
        ).strip()
        if actual != expected:
            raise ValueError("Prepared native source pin differs")
    verify_files(ROOT, profile.sdk_files)
    verify_files(native, profile.native_files)
    runtime = ROOT / "src/pybeam/runtime/_assets"
    actual_files = {str(path.relative_to(runtime)) for path in runtime.rglob("*") if path.is_file()}
    if actual_files != profile.runtime_files.keys():
        raise ValueError("Prepared runtime has missing or undeclared files")
    verify_files(runtime, profile.runtime_files)
    nif = runtime / "lib/erlang_python-5.0.1/priv/py_nif.so"
    if digest(nif) != profile.staged_nif_sha256:
        raise ValueError("Prepared NIF pin differs")
    packaged_assets(runtime)
    manifest = verify_manifest(runtime / "optional/genstage")
    if len(manifest.files) != 368 or sum(len(app.modules) for app in manifest.applications) != 357:
        raise ValueError("Prepared optional application set differs")


def unavailable() -> str | None:
    try:
        verify_prepared()
    except OSError, ValueError, subprocess.SubprocessError:
        return "Verified prepared distribution profile is unavailable. No runtime is launched."
    return None
