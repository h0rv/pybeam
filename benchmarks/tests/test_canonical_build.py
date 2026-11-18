import os
import subprocess
from pathlib import Path

import pytest

from benchmarks import canonical_build
from benchmarks.contract import Source
from benchmarks.provenance import source
from benchmarks.pybeam_reference_build import BASELINE, BuildManifest, locked_revision, verify


def test_lock__selects_native_entry_not_another_dependency(tmp_path: Path) -> None:
    lock = tmp_path / "rebar.lock"
    lock.write_text(
        '[{<<"other">>, {git,"url",{ref,"' + "0" * 40 + '"}},0},'
        '{<<"erlang_python">>, {git,"url",{ref,"' + BASELINE + '"}},0}].'
    )
    assert locked_revision(lock) == BASELINE


@pytest.mark.parametrize("contents", ["[]", '{<<"erlang_python">>, {git,"url",{branch,"main"}}}'])
def test_lock__rejects_missing_or_unpinned_native(tmp_path: Path, contents: str) -> None:
    lock = tmp_path / "rebar.lock"
    lock.write_text(contents)
    with pytest.raises(ValueError, match="one pinned"):
        locked_revision(lock)


@pytest.fixture
def canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Source, list[tuple[str, ...]]]:
    native = tmp_path / canonical_build.NATIVE_DIRECTORY
    native.mkdir(parents=True)
    (tmp_path / "rebar.lock").write_text(
        '[{<<"erlang_python">>, {git,"url",{ref,"' + BASELINE + '"}},0}].'
    )
    fingerprint = Source(
        path=str(native),
        sha=BASELINE,
        dirty=False,
        diff_sha256="0" * 64,
        working_tree_sha256="0" * 64,
    )

    def source(_path: Path, *, generated_paths: frozenset[str] = frozenset()) -> Source:
        return fingerprint

    commands: list[tuple[str, ...]] = []

    def compile(
        command: tuple[str, ...], *, cwd: Path, check: bool
    ) -> subprocess.CompletedProcess[bytes]:
        assert cwd == tmp_path and check
        commands.append(command)
        for name in (
            "priv/py_nif.so",
            "priv/_erlang_impl/__init__.py",
            "ebin/py_launch.beam",
            "ebin/erlang_python.app",
            "_build/cmake/CMakeCache.txt",
        ):
            path = native / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("built asset")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(canonical_build.subprocess, "run", compile)
    monkeypatch.setattr(canonical_build, "source", source)
    monkeypatch.setattr("benchmarks.pybeam_reference_build.source", source)
    return native, fingerprint, commands


def test_canonical__runs_builder_and_records_assets(
    canonical: tuple[Path, Source, list[tuple[str, ...]]], tmp_path: Path
) -> None:
    native, fingerprint, commands = canonical
    path = canonical_build.build(tmp_path)
    manifest = BuildManifest.model_validate_json(path.read_text())
    assert manifest.native == fingerprint
    assert manifest.runtime_directory == str(native)
    assert manifest.commands == tuple(commands)
    assert commands[0][1] == str(tmp_path / "tools/build_native.py")
    assert len(manifest.assets) == 4
    verify(manifest, native)


@pytest.mark.parametrize("damage", ["nif", "cache", "helper", "source", "revision", "escape"])
def test_canonical__rejects_stale_or_escaped_build(
    canonical: tuple[Path, Source, list[tuple[str, ...]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    native, fingerprint, _ = canonical
    manifest = BuildManifest.model_validate_json(canonical_build.build(tmp_path).read_text())
    match damage:
        case "nif":
            (native / "priv/py_nif.so").unlink()
        case "cache":
            (native / "_build/cmake/CMakeCache.txt").write_text("changed cache")
        case "helper":
            (native / "priv/_erlang_impl/__init__.py").write_text("changed helper")
        case "source" | "revision":
            changed = fingerprint.model_copy(
                update={"working_tree_sha256": "1" * 64}
                if damage == "source"
                else {"sha": "1" * 40}
            )

            def source(_path: Path, *, generated_paths: frozenset[str] = frozenset()) -> Source:
                return changed

            monkeypatch.setattr("benchmarks.pybeam_reference_build.source", source)
        case "escape":
            manifest = manifest.model_copy(update={"runtime_directory": str(tmp_path.parent)})
    with pytest.raises((ValueError, OSError)):
        verify(manifest, native)


@pytest.mark.parametrize("name", ["_build", "priv", "ebin", "_checkouts/erlang_python"])
def test_canonical__refuses_escaping_or_overridden_build_before_compilation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "project"
    root.mkdir()
    path = root / name if name.startswith("_") else root / canonical_build.NATIVE_DIRECTORY / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Unsafe paths must fail before compilation")

    monkeypatch.setattr(canonical_build.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match=r"checkout|escapes"):
        canonical_build.build(root)


def test_recorded_manifest__can_verify_its_original_revision_explicitly(
    canonical: tuple[Path, Source, list[tuple[str, ...]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native, fingerprint, _ = canonical
    manifest = BuildManifest.model_validate_json(canonical_build.build(tmp_path).read_text())
    original = fingerprint.model_copy(update={"sha": "a1ca04bfcaea817654ec7d1972f210c7a60075c4"})
    manifest = manifest.model_copy(update={"native": original})

    def source(_path: Path, *, generated_paths: frozenset[str] = frozenset()) -> Source:
        return original

    monkeypatch.setattr("benchmarks.pybeam_reference_build.source", source)
    with pytest.raises(ValueError, match="pinned"):
        verify(manifest, native)
    verify(manifest, native, expected_revision=original.sha)


def test_source__excludes_only_untracked_declared_outputs(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603,S607
            ("git", "-C", str(tmp_path), *args),
            check=True,
            capture_output=True,
            env=dict(
                os.environ,
                GIT_AUTHOR_NAME="Fixture",
                GIT_AUTHOR_EMAIL="fixture@example.invalid",
                GIT_COMMITTER_NAME="Fixture",
                GIT_COMMITTER_EMAIL="fixture@example.invalid",
            ),
        )

    git("init", "--initial-branch=main")
    tracked = tmp_path / "source.erl"
    tracked.write_text("original source")
    git("add", "source.erl")
    git("commit", "-m", "fixture")
    before = source(tmp_path)
    app = tmp_path / "ebin/erlang_python.app"
    app.parent.mkdir()
    app.write_text("generated application metadata")
    outputs = frozenset({"ebin/erlang_python.app"})
    assert source(tmp_path).dirty
    assert source(tmp_path, generated_paths=outputs) == before
    (app.parent / "unexpected.app").write_text("not declared")
    assert source(tmp_path, generated_paths=outputs).dirty
    tracked.write_text("modified source")
    assert (
        source(tmp_path, generated_paths=outputs).working_tree_sha256 != before.working_tree_sha256
    )
    with pytest.raises(ValueError, match="Tracked source"):
        source(tmp_path, generated_paths=frozenset({"source.erl"}))
