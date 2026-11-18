import subprocess
import sys
from pathlib import Path

from benchmarks.provenance import source
from benchmarks.pybeam_reference_build import (
    NATIVE_DIRECTORY,
    ROOT,
    BuildManifest,
    asset,
    locked_revision,
    verify,
)


def dependency(root: Path) -> Path:
    native = (root / NATIVE_DIRECTORY).resolve()
    build_root = (root / "_build").resolve()
    if not build_root.is_relative_to(root.resolve()) or not native.is_relative_to(build_root):
        raise ValueError("Canonical benchmark build requires a rebar-fetched native checkout")
    for name in ("c_src", "src", "priv", "ebin", "_build"):
        directory = native / name
        if not directory.resolve().is_relative_to(native):
            raise ValueError("Canonical native output escapes its checkout: " + str(directory))
        for path in directory.rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(native):
                raise ValueError("Canonical native output escapes its checkout: " + str(path))
    return native


def build(root: Path = ROOT) -> Path:
    dependency(root)
    override = root / "_checkouts/erlang_python"
    if override.exists() or override.is_symlink():
        raise ValueError(
            "Remove the erlang_python checkout override before a public benchmark build"
        )
    command = (sys.executable, str(root / "tools/build_native.py"))
    subprocess.run(command, cwd=root, check=True)  # noqa: S603
    native = dependency(root)
    before = source(native, generated_paths=frozenset({"ebin/erlang_python.app"}))
    if before.dirty or before.sha != locked_revision(root / "rebar.lock"):
        raise ValueError("Canonical native source is not clean at the locked revision")
    beams = sorted((native / "ebin").glob("*.beam"))
    helpers = sorted((native / "priv/_erlang_impl").glob("*.py"))
    if not beams or not helpers:
        raise ValueError("Canonical native build is missing BEAM or Python helper assets")
    manifest = BuildManifest(
        native=before,
        runtime_directory=str(native),
        python_executable=asset(Path(sys.executable)),
        cmake_cache=asset(native / "_build/cmake/CMakeCache.txt"),
        assets=tuple(
            asset(path)
            for path in (
                native / "priv/py_nif.so",
                *beams,
                native / "ebin/erlang_python.app",
                *helpers,
            )
        ),
        commands=(command,),
    )
    verify(manifest, native)
    path = native / "_build/pybeam-reference-build.json"
    path.write_text(manifest.model_dump_json(indent=2) + "\n")
    verify(manifest, native)
    return path


def main() -> None:
    print(build())


if __name__ == "__main__":
    main()
