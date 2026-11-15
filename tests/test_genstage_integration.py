import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from examples.genstage_callbacks import process
from pybeam.integrations.genstage.pipeline import stream
from pybeam.runtime import optional, optional_manifest
from pybeam.runtime.config import BootstrapError
from pybeam.runtime.optional import read_manifest, start_optional
from pybeam.runtime.optional_manifest import (
    ApplicationAsset,
    AssetFile,
    ModuleIdentity,
    OptionalManifest,
    Sources,
    approved_manifest,
    write_manifest,
)
from tools.runtime_wheel import audit_optional


@dataclass(frozen=True)
class Artifact:
    root: Path
    approved: OptionalManifest


@pytest.fixture
def artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Artifact:
    root = tmp_path / "_assets/optional/example"
    directory = root / "lib/example-1.0/ebin"
    directory.mkdir(parents=True)
    app = directory / "example.app"
    app.write_text('{application, example, [{vsn, "1.0"}, {modules, [example]}]}.')
    beam = directory / "example.beam"
    beam.write_bytes(b"filesystem validation fixture")
    pin = OptionalManifest(
        format_version=1,
        name="example",
        otp_version="29.1.2",
        erts_version="17.1.1",
        sources=Sources(
            elixir="853b7cd719bce7df87be4271ee91c90acdf706e1",
            gen_stage="d1532fa56482bc92d90abc777439ff8be34b5b1a",
        ),
        applications=(
            ApplicationAsset(
                name="example",
                version="1.0",
                code_path=Path("lib/example-1.0/ebin"),
                dependencies=(),
                modules=(ModuleIdentity(name="example", md5="0" * 32),),
            ),
        ),
        files=tuple(
            AssetFile(
                path=path.relative_to(root), sha256=hashlib.sha256(path.read_bytes()).hexdigest()
            )
            for path in (app, beam)
        ),
    )
    write_manifest(root, pin)

    def approved(name: str) -> OptionalManifest:
        if name != "example":
            raise ValueError("Unknown optional artifact")
        return pin

    monkeypatch.setattr(optional_manifest, "approved_manifest", approved)
    return Artifact(root, pin)


def rejected(root: Path) -> None:
    with pytest.raises(BootstrapError, match="Optional runtime artifact"):
        read_manifest(root)
    with pytest.raises((ValueError, OSError)):
        audit_optional(root)


@pytest.mark.asyncio
async def test_optional_activation_requires_the_managed_entry_loop() -> None:
    with pytest.raises(BootstrapError, match="require a running managed Python entry"):
        await start_optional("genstage")


def test_missing_optional_artifact(tmp_path: Path) -> None:
    rejected(tmp_path)


def test_approved_release_is_the_pinned_build() -> None:
    pin = approved_manifest("genstage")
    assert (pin.otp_version, pin.erts_version) == ("29.1.2", "17.1.1")
    assert [(app.name, app.version) for app in pin.applications] == [
        ("compiler", "10.0.7"),
        ("elixir", "1.20.0"),
        ("gen_stage", "1.3.2"),
        ("logger", "1.20.0"),
        ("pybeam_genstage", "0.1.0"),
    ]
    assert len({item.path for item in pin.files}) == len(pin.files)
    for app in pin.applications:
        expected = {app.code_path / (module.name + ".beam") for module in app.modules}
        actual = {
            item.path
            for item in pin.files
            if item.path.parent == app.code_path and item.path.suffix == ".beam"
        }
        assert actual == expected


def test_manifest_verifies_all_files(artifact: Artifact) -> None:
    assert read_manifest(artifact.root) == artifact.approved
    audit_optional(artifact.root)
    (artifact.root / "lib/example-1.0/ebin/example.beam").write_bytes(b"modified")
    rejected(artifact.root)


def test_undeclared_code_fails_closed(artifact: Artifact) -> None:
    (artifact.root / "lib/example-1.0/ebin/unknown.beam").touch()
    rejected(artifact.root)


def test_hash_declared_extra_module_is_not_approval(artifact: Artifact) -> None:
    extra = artifact.root / "lib/example-1.0/ebin/erl_eval.beam"
    extra.write_bytes(b"not approved")
    manifest = artifact.approved.model_copy(
        update={
            "files": (
                *artifact.approved.files,
                AssetFile(
                    path=extra.relative_to(artifact.root),
                    sha256=hashlib.sha256(extra.read_bytes()).hexdigest(),
                ),
            )
        }
    )
    write_manifest(artifact.root, manifest)
    rejected(artifact.root)


def test_hash_declared_extra_application_is_not_approval(artifact: Artifact) -> None:
    directory = artifact.root / "lib/extra-1.0/ebin"
    directory.mkdir(parents=True)
    app = directory / "extra.app"
    beam = directory / "extra.beam"
    app.write_text('{application, extra, [{vsn, "1.0"}, {modules, [extra]}]}.')
    beam.write_bytes(b"extra application")
    extra = ApplicationAsset(
        name="extra",
        version="1.0",
        code_path=directory.relative_to(artifact.root),
        dependencies=(),
        modules=(ModuleIdentity(name="extra", md5="0" * 32),),
    )
    files = tuple(
        AssetFile(
            path=path.relative_to(artifact.root),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in (app, beam)
    )
    candidate = artifact.approved.model_copy(
        update={
            "applications": (*artifact.approved.applications, extra),
            "files": (*artifact.approved.files, *files),
        }
    )
    write_manifest(artifact.root, candidate)
    rejected(artifact.root)


def test_symlinked_code_fails_closed(artifact: Artifact, tmp_path: Path) -> None:
    beam = artifact.root / "lib/example-1.0/ebin/example.beam"
    target = tmp_path / "outside.beam"
    target.write_bytes(beam.read_bytes())
    beam.unlink()
    beam.symlink_to(target)
    rejected(artifact.root)


@pytest.mark.asyncio
async def test_external_manifest_is_rejected_before_activation(
    artifact: Artifact, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "outside-manifest.json"
    target.write_bytes((artifact.root / "manifest.json").read_bytes())
    (artifact.root / "manifest.json").unlink()
    (artifact.root / "manifest.json").symlink_to(target)
    rejected(artifact.root)
    monkeypatch.setattr(optional, "require_managed_entry", lambda: None)
    monkeypatch.setattr(optional, "__file__", str(tmp_path / "optional.py"))
    with pytest.raises(BootstrapError, match="Optional runtime artifact"):
        await start_optional("example")


@pytest.mark.parametrize(
    "field,value",
    [
        ("otp_version", "0.0.0"),
        ("erts_version", "0.0.0"),
        ("name", "not_approved"),
        ("sources", {"elixir": "0" * 40, "gen_stage": "0" * 40}),
        ("format_version", True),
        ("format_version", 1.0),
        ("format_version", "1"),
        ("otp_version", 29),
        ("otp_version", False),
        ("unknown", 1),
    ],
)
def test_fabricated_or_ill_typed_metadata_fails_both_readers(
    artifact: Artifact, field: str, value: object
) -> None:
    payload: object = json.loads(artifact.approved.model_dump_json())
    assert isinstance(payload, dict)
    payload[field] = value
    (artifact.root / "manifest.json").write_text(json.dumps(payload))
    rejected(artifact.root)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "0.0.0"),
        ("code_path", "../../elsewhere"),
        ("code_path", "lib/kernel-1/ebin"),
        ("dependencies", ["unknown"]),
        ("dependencies", [True]),
        ("dependencies", [1]),
        ("dependencies", [1.0]),
        ("dependencies", True),
        ("modules", []),
        ("modules", [{"name": "unapproved", "md5": "0" * 32}]),
        ("unknown", "ignored"),
    ],
)
def test_application_contract_is_approved_not_self_declared(
    artifact: Artifact, field: str, value: object
) -> None:
    payload: object = json.loads(artifact.approved.model_dump_json())
    assert isinstance(payload, dict)
    apps: object = payload["applications"]
    assert isinstance(apps, list)
    app: object = apps[0]
    assert isinstance(app, dict)
    app[field] = value
    (artifact.root / "manifest.json").write_text(json.dumps(payload))
    rejected(artifact.root)


@pytest.mark.asyncio
async def test_unsupported_hard_bound() -> None:
    with pytest.raises(NotImplementedError, match="hard graph memory bound"):
        async with stream(16, process, max_bytes=1024):
            pytest.fail("unsupported bound was accepted")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, -1, 17, True])
async def test_count_rejected_before_native_import(count: int) -> None:
    with pytest.raises(ValueError, match="count must be"):
        async with stream(count, process):
            pytest.fail("unsupported count was accepted")


def test_core_does_not_load_genstage() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, pybeam; assert 'pybeam.integrations.genstage' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
