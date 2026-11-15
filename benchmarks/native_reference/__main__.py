import argparse
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from benchmarks.contract import Config, LoadedAsset, Payload, StrictModel, Unsupported

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "_build"


class BuildManifest(StrictModel):
    otp: str
    otp_source_tag: str
    compiler: LoadedAsset
    sources: tuple[LoadedAsset, ...]
    assets: tuple[LoadedAsset, ...]
    pybeam_nif_loaded: bool = False
    paired_native_checkout_used: bool = False


def asset(path: Path) -> LoadedAsset:
    resolved = path.resolve(strict=True)
    return LoadedAsset(path=str(resolved), sha256=hashlib.sha256(resolved.read_bytes()).hexdigest())


def executable(name: str) -> Path:
    located = shutil.which(name)
    if located is None:
        raise RuntimeError("Missing OTP executable: " + name)
    return Path(located).resolve(strict=True)


def build() -> None:
    compiler = executable("erlc")
    otp_file = compiler.parent.parent / "releases/29/OTP_VERSION"
    version = otp_file.read_text().strip()
    if version != "29.1.2":
        raise RuntimeError("This reference targets OTP 29.1.2, found " + version)
    BUILD.mkdir(exist_ok=True)
    sources = tuple(ROOT / name for name in ("room.erl", "native_bench.erl"))
    environment = dict(os.environ, ERL_FLAGS="+S 1:1 +A 1 +SDcpu 1 +SDio 1")
    # One compiler invocation, no detached session, no NIF build.
    subprocess.run(  # noqa: S603
        [str(compiler), "+debug_info", "-Werror", "-o", str(BUILD), *map(str, sources)],
        check=True,
        env=environment,
        timeout=30,
    )
    manifest = BuildManifest(
        otp=version,
        otp_source_tag="https://github.com/erlang/otp/tree/OTP-29.1.2",
        compiler=asset(compiler),
        sources=tuple(map(asset, sources)),
        assets=tuple(asset(BUILD / name) for name in ("room.beam", "native_bench.beam")),
    )
    (BUILD / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n")


def unsupported(config: Config) -> Unsupported | None:
    w = config.workload
    if config.backend != "erlang" or w.id != "rooms.local" or w.activity != "uniform":
        return Unsupported(reason="Only uniform rooms.local is implemented", features=(w.id,))
    if w.payload != Payload():
        return Unsupported(
            reason="Only v1 scalar JSON fixture 1 is implemented", features=("payload",)
        )
    if w.queue_bound_scope != "client_admission_only":
        return Unsupported(
            reason="Standard gen_server mailboxes are unbounded; no destination queue bound",
            features=("destination_queue",),
        )
    if config.unsafe_native_crash:
        return Unsupported(
            reason="Native crash probes are not implemented", features=("native_crash",)
        )
    return None


class Arguments(argparse.Namespace):
    build: bool = False
    probe_failure: bool = False
    directory: Path | None = None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--probe-failure", action="store_true")
    parser.add_argument("directory", type=Path, nargs="?")
    args = parser.parse_args(namespace=Arguments())
    if args.build:
        build()
        return
    directory: Path | None = args.directory
    if directory is None:
        parser.error("A guardian-owned trial directory is required")
    config = Config.model_validate_json((directory / "config.json").read_text())
    rejected = unsupported(config)
    if rejected is not None:
        (directory / "adapter.json").write_text(rejected.model_dump_json() + "\n")
        return
    BuildManifest.model_validate_json((BUILD / "manifest.json").read_text())
    erl = executable("erl")
    # Replace the registered adapter itself, preserving guardian PID ownership.
    # No subprocess, new session, detached VM, or Python per-request coordinator.
    os.execv(  # noqa: S606  Guardian-owned primary process replacement.
        str(erl),
        [
            str(erl),
            "+S",
            "1:1",
            "+A",
            "1",
            "+SDcpu",
            "1",
            "+SDio",
            "1",
            "-noshell",
            "-noinput",
            "-pa",
            str(BUILD),
            "-s",
            "native_bench",
            "main",
            "-extra",
            str(directory.resolve()),
            "failure" if args.probe_failure else "run",
        ],
    )


if __name__ == "__main__":
    main()
