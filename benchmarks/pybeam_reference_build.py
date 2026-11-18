import argparse
import os
import re
import shutil
import subprocess
from pathlib import Path

from tools.build_native import compiler_environment

from benchmarks.contract import LoadedAsset, Source, StrictModel
from benchmarks.provenance import source

ROOT = Path(__file__).resolve().parents[1]
NATIVE_DIRECTORY = Path("_build/hornbeam/lib/erlang_python")


def locked_revision(lock: Path = ROOT / "rebar.lock") -> str:
    pattern: re.Pattern[str] = re.compile(
        r'\{<<"erlang_python">>,\s*\{git,\s*"[^"]+",\s*'
        r'\{ref,\s*"([0-9a-f]{40})"\}\}'
    )
    entries = tuple(pattern.finditer(lock.read_text()))
    if len(entries) != 1:
        raise ValueError("Expected one pinned erlang_python Git revision in rebar.lock")
    return entries[0].group(1)


BASELINE = locked_revision()
CANONICAL_NATIVE = Path(__file__).resolve().parents[2] / "erlang-python"


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


def verify(manifest: BuildManifest, native: Path, *, expected_revision: str | None = None) -> None:
    native = native.resolve(strict=True)
    generated = generated_paths(manifest, native)
    current = source(native, generated_paths=generated) if generated else source(native)
    if (
        current != manifest.native
        or current.dirty
        or current.sha != (locked_revision() if expected_revision is None else expected_revision)
    ):
        raise ValueError("Native source does not match the clean pinned current runtime build")
    for path in (
        Path(manifest.runtime_directory),
        Path(manifest.cmake_cache.path),
        *(Path(item.path) for item in manifest.assets),
    ):
        if not path.is_absolute() or not path.resolve().is_relative_to(native):
            raise ValueError("Build asset escapes the native checkout: " + str(path))
    if not manifest.assets:
        raise ValueError("Build manifest has no native assets")
    for item in (*manifest.assets, manifest.python_executable, manifest.cmake_cache):
        if asset(Path(item.path)) != item:
            raise ValueError("Build asset changed: " + item.path)


def generated_paths(manifest: BuildManifest, native: Path) -> frozenset[str]:
    path = native / "ebin/erlang_python.app"
    if Path(manifest.runtime_directory).resolve() != native or not any(
        Path(item.path) == path for item in manifest.assets
    ):
        return frozenset()
    if asset(path) not in manifest.assets:
        raise ValueError("Build asset changed: " + str(path))
    return frozenset({"ebin/erlang_python.app"})


def private_outputs(native: Path) -> None:
    if native == CANONICAL_NATIVE.resolve():
        raise ValueError("Benchmark preparation refuses the canonical native checkout")
    for name in (
        "c_src",
        "src",
        "priv",
        "priv/py_nif.so",
        "_build",
        "_build/cmake",
        "_build/cmake/CMakeCache.txt",
        "_build/default/lib/erlang_python",
        "_build/default/lib/erlang_python/ebin",
        "_build/default/lib/erlang_python/priv",
        "_build/pybeam-reference-build.json",
    ):
        if not (native / name).resolve().is_relative_to(native):
            raise ValueError("Private build path escapes the selected checkout: " + name)
    for directory in (native / "priv", native / "_build"):
        for path in directory.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(native):
                raise ValueError(
                    "Private build output symlink escapes the selected checkout: " + str(path)
                )


def build(native: Path, python: Path) -> Path:
    native = native.resolve(strict=True)
    private_outputs(native)
    before = source(native)
    if before.dirty or before.sha != BASELINE:
        raise ValueError("Build requires the clean pinned current native source")
    jobs = os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL") or "2"
    if jobs not in {"1", "2"}:
        raise ValueError("CMAKE_BUILD_PARALLEL_LEVEL must be 1 or 2")
    python = python.resolve(strict=True)
    python_config = python.with_name("python3.14-config")
    if not python_config.is_file():
        raise ValueError("Selected Python must provide its matching python3.14-config")
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
        ("cmake", "--build", str(native / "_build/cmake"), "--parallel", jobs),
        ("erlc", "-o", str(ebin), *map(str, sorted((native / "src").glob("*.erl")))),
    )
    environment = dict(
        compiler_environment(),
        ERL_FLAGS="+S 1:1 +A 1",
        PYTHON_CONFIG=str(python_config),
        CMAKE_BUILD_PARALLEL_LEVEL=jobs,
    )
    for command in commands:
        private_outputs(native)
        subprocess.run(command, env=environment, check=True, timeout=240)  # noqa: S603
    private_outputs(native)
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
