import shutil
from pathlib import Path
from typing import Literal

from benchmarks.contract import Config
from benchmarks.native_reference.__main__ import BUILD
from benchmarks.native_reference.__main__ import BuildManifest as OTPBuildManifest
from benchmarks.native_reference.__main__ import asset as otp_asset
from benchmarks.pybeam_reference_build import BuildManifest, verify

type BenchmarkName = Literal["python", "erlang", "pybeam"]
ROOT = Path(__file__).resolve().parent.parent
NAMES: tuple[BenchmarkName, ...] = ("python", "erlang", "pybeam")


def configuration(name: BenchmarkName, native: Path | None = None) -> Config:
    config = Config.model_validate_json(
        (ROOT / "benchmarks/examples" / ("serial_" + name + ".json")).read_text()
    )
    return Config.model_validate_json(
        config.model_copy(
            update={
                "python_source": str(ROOT),
                "native_source": str(native.resolve())
                if name == "pybeam" and native is not None
                else config.native_source,
                "repeats": 1,
                "adapter_command": (
                    str(ROOT / "benchmarks/.venv/bin/python"),
                    *config.adapter_command[1:],
                ),
            }
        ).model_dump_json()
    )


def unavailable(name: BenchmarkName, native: Path | None) -> str | None:
    if not (ROOT / ".git").exists():
        return "Source archive lacks required Git provenance. Benchmark launch disabled."
    if name != "python" and shutil.which("escript" if name == "pybeam" else "erl") is None:
        return "Required OTP executable unavailable. No automatic install or build."
    if not (ROOT / "benchmarks/.venv/bin/python").is_file():
        return "Benchmark environment missing. Run uv sync --project benchmarks explicitly."
    if name == "erlang":
        try:
            manifest = OTPBuildManifest.model_validate_json((BUILD / "manifest.json").read_text())
            for item in (*manifest.assets, *manifest.sources, manifest.compiler):
                if otp_asset(Path(item.path)) != item:
                    return "OTP reference assets changed. Rebuild the reference explicitly."
        except OSError, ValueError:
            return (
                "OTP reference assets missing. Run python -m benchmarks.native_reference --build."
            )
    if name == "pybeam":
        if native is None:
            return "No trusted private native checkout configured. Use --trusted-native-root."
        try:
            manifest = BuildManifest.model_validate_json(
                (native / "_build/pybeam-reference-build.json").read_text()
            )
            verify(manifest, native.resolve())
        except OSError, ValueError:
            return "Matching current native build assets unavailable or changed. Build explicitly."
    return None
