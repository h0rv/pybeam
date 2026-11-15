import argparse
import os
import shutil
import subprocess
from pathlib import Path

from benchmarks.contract import LoadedAsset, Source, StrictModel
from benchmarks.provenance import source

BASELINE = "9baab2f4c16e37fab0c98dd3417c61f0d8f4a4d3"


class BuildManifest(StrictModel):
    native: Source
    runtime_directory: str
    python_executable: LoadedAsset
    cmake_cache: LoadedAsset
    assets: tuple[LoadedAsset, ...]
    commands: tuple[tuple[str, ...], ...]


def asset(path: Path) -> LoadedAsset:
    import hashlib

    return LoadedAsset(
        path=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )


def verify(manifest: BuildManifest, native: Path) -> None:
    current = source(native)
    if current != manifest.native or current.dirty or current.sha != BASELINE:
        raise ValueError("Native source does not match the clean legacy baseline build")
    for item in (*manifest.assets, manifest.python_executable, manifest.cmake_cache):
        if asset(Path(item.path)) != item:
            raise ValueError("Build asset changed: " + item.path)


def build(native: Path, python: Path) -> Path:
    before = source(native)
    if before.dirty or before.sha != BASELINE:
        raise ValueError("Build requires the clean pinned legacy native source")
    checkout = Path(__file__).resolve().parent.parent / "_checkouts/erlang_python"
    checkout.parent.mkdir(parents=True, exist_ok=True)
    if not checkout.exists():
        checkout.symlink_to(native, target_is_directory=True)
    if checkout.resolve() != native:
        raise ValueError("Private native checkout configuration points elsewhere")
    runtime = native / "_build/default/lib/erlang_python"
    ebin = runtime / "ebin"
    ebin.mkdir(parents=True, exist_ok=True)
    priv = runtime / "priv"
    if not priv.exists():
        priv.symlink_to(native / "priv", target_is_directory=True)
    if priv.resolve() != native / "priv":
        raise ValueError("Unexpected private runtime priv directory")
    commands = (
        (
            "cmake",
            "-S",
            str(native / "c_src"),
            "-B",
            str(native / "_build/cmake"),
            "-DPython3_EXECUTABLE=" + str(python),
            "-DCMAKE_BUILD_TYPE=Release",
        ),
        ("cmake", "--build", str(native / "_build/cmake"), "--parallel", "2"),
        ("erlc", "-o", str(ebin), *map(str, sorted((native / "src").glob("*.erl")))),
    )
    environment = dict(os.environ, ERL_FLAGS="+S 1:1 +A 1")
    for command in commands:
        subprocess.run(command, env=environment, check=True, timeout=240)  # noqa: S603
    shutil.copyfile(native / "src/erlang_python.app.src", ebin / "erlang_python.app")
    manifest = BuildManifest(
        native=before,
        runtime_directory=str(runtime),
        python_executable=asset(python),
        cmake_cache=asset(native / "_build/cmake/CMakeCache.txt"),
        assets=tuple(
            asset(path)
            for path in (
                native / "priv/py_nif.so",
                *sorted(ebin.glob("*.beam")),
                ebin / "erlang_python.app",
                *sorted((native / "priv/_erlang_impl").glob("*.py")),
            )
        ),
        commands=commands,
    )
    verify(manifest, native)
    path = native / "_build/pybeam-reference-build.json"
    path.write_text(manifest.model_dump_json(indent=2) + "\n")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("native", type=Path)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.native.resolve(), args.python.resolve()))


if __name__ == "__main__":
    main()
