import argparse
import hashlib
import json
import shutil
import stat
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from pydantic import BaseModel, ConfigDict

from pybeam.runtime.assets import MANIFEST
from pybeam.runtime.optional_manifest import contained
from tools.runtime_wheel import runtime_tag

APPROVED_WHEEL = "75192f28d0ba1fc7e021662ba26daf50430b409267179dee50cb99ccbe9269e2"
SDK_REVISION = "30ed3d8844aeceaba1cf4eb549f1013aea9f5d38"
NATIVE_REVISION = "3234b697c4fcec478858aac99a320583439f40a3"
HELPER_SHA256 = "0db382fb5590a404d19d1734372d8d91df93abf599a5f996bc73461b24e71671"
NIF_SHA256 = "4a106fb8c6cf562cef7bfd884b0656fcbb5aba338014d0fcafab1e7d1033381a"
PREFIX = "pybeam/runtime/_assets/"


class Provenance(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    artifact_sdk_revision: str
    artifact_native_revision: str
    wheel_sha256: str
    payloads: dict[str, str]
    runtime_payloads: dict[str, str]
    preserved_hornbeam_payloads: dict[str, str]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preserve_hornbeam(source: Path, runtime: Path) -> dict[str, str]:
    manifest = MANIFEST.decode((source / "runtime.json").read_bytes())
    code_path = Path("lib/hornbeam-1.4.1/ebin")
    if code_path not in manifest.code_paths:
        raise ValueError("The explicitly selected runtime must contain Hornbeam 1.4.1")
    paths = sorted(contained(source, code_path.parent).rglob("*"))
    paths.append(contained(source, Path("notices/hornbeam-LICENSE")))
    hashes: dict[str, str] = {}
    for path in paths:
        relative = path.relative_to(source)
        contained(source, relative)
        if path.is_file():
            destination = runtime / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            hashes[str(relative)] = digest(destination)
    core = MANIFEST.decode((runtime / "runtime.json").read_bytes())
    merged = replace(core, code_paths=(*core.code_paths, code_path))
    (runtime / "runtime.json").write_bytes(MANIFEST.encode(merged) + b"\n")
    return hashes


def stage(wheel: Path, output: Path, *, hornbeam_runtime: Path | None = None) -> Path:
    """Extract only the exact reviewed wheel into a new generation, never replace live assets.

    The provenance names the artifact's source revisions, not the invoking checkout.
    An optional existing Hornbeam payload is copied unchanged and recorded separately.
    Nothing is compiled, downloaded, installed, or written through an existing inode.
    """
    wheel = wheel.resolve(strict=True)
    if digest(wheel) != APPROVED_WHEEL:
        raise ValueError("Expected the exact reviewed GenStage wheel SHA256: " + APPROVED_WHEEL)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError("Asset preparation requires a new output directory: " + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="genstage-stage-", dir=output.parent) as temporary:
        generation = Path(temporary)
        runtime = generation / "runtime"
        payloads: dict[str, str] = {}
        with ZipFile(wheel) as archive:
            members = archive.infolist()
            if len(members) > 4096 or sum(item.file_size for item in members) > 268_435_456:
                raise ValueError("Wheel exceeds supported extraction limits")
            for item in members:
                name = PurePosixPath(item.filename)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or stat.S_ISLNK(item.external_attr >> 16)
                ):
                    raise ValueError("Wheel contains an unsafe path")
                if item.is_dir():
                    continue
                if item.filename in payloads:
                    raise ValueError("Wheel contains a duplicate file")
                data = archive.read(item)
                payloads[item.filename] = hashlib.sha256(data).hexdigest()
                if item.filename.startswith(PREFIX):
                    relative = Path(item.filename.removeprefix(PREFIX))
                    destination = contained(runtime, relative)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                    destination.chmod((item.external_attr >> 16) & 0o777)
        native = runtime / "lib/erlang_python-5.0.1"
        if digest(native / "ebin/py_application.beam") != HELPER_SHA256:
            raise ValueError("Reviewed native application helper is missing or changed")
        if digest(native / "priv/py_nif.so") != NIF_SHA256:
            raise ValueError("Reviewed core NIF is missing or changed")
        preserved = (
            {}
            if hornbeam_runtime is None
            else preserve_hornbeam(hornbeam_runtime.resolve(), runtime)
        )
        runtime_tag(runtime)
        provenance = Provenance(
            artifact_sdk_revision=SDK_REVISION,
            artifact_native_revision=NATIVE_REVISION,
            wheel_sha256=APPROVED_WHEEL,
            payloads=payloads,
            runtime_payloads={
                str(path.relative_to(runtime)): digest(path)
                for path in sorted(runtime.rglob("*"))
                if path.is_file()
            },
            preserved_hornbeam_payloads=preserved,
        )
        (generation / "provenance.json").write_text(provenance.model_dump_json(indent=2) + "\n")
        if output.exists() or output.is_symlink():
            raise FileExistsError("Output appeared during preparation: " + str(output))
        generation.rename(output)
    return output / "runtime"


class Arguments(argparse.Namespace):
    wheel: Path
    output: Path
    preserve_hornbeam_from: Path | None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare verified prebuilt assets without C builds"
    )
    parser.add_argument("wheel", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--preserve-hornbeam-from", type=Path)
    args = parser.parse_args(namespace=Arguments())
    runtime = stage(args.wheel, args.output, hornbeam_runtime=args.preserve_hornbeam_from)
    print(json.dumps({"runtime": str(runtime), "approved_wheel_sha256": APPROVED_WHEEL}))


if __name__ == "__main__":
    main()
