import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from pybeam.runtime.assets import RuntimeManifest
from pybeam.runtime.optional_manifest import (
    ApplicationAsset,
    AssetFile,
    OptionalManifest,
    Sources,
    approved_manifest,
    verify_manifest,
    write_manifest,
)
from tools.build_runtime import run

# The BEAM bytes embed this path, so every build uses the same one.
WORKSPACE = Path("/tmp/pybeam-genstage")  # noqa: S108
BRIDGE = Path(__file__).parents[1] / "src/pybeam/integrations/genstage"
# Mix loads Hex to read GenStage's lockfile; Hex is not part of the payload.
HEX_VERSION = "2.7.0"
REPOSITORIES = {
    "elixir": "https://github.com/elixir-lang/elixir.git",
    "gen_stage": "https://github.com/elixir-lang/gen_stage.git",
}


@dataclass(frozen=True, slots=True)
class Checkouts:
    elixir: Path
    genstage: Path
    bridge: Path


def pinned(root: Path, revision: str) -> None:
    actual = run(["git", "-C", str(root), "rev-parse", "HEAD"]).strip()
    if actual != revision:
        raise ValueError("Unexpected optional dependency revision: " + str(root))
    run(["git", "-C", str(root), "diff", "--quiet", "HEAD"])


def checkout(root: Path, repository: str, revision: str) -> None:
    run(["git", "init", "-q", str(root)])
    run(["git", "-C", str(root), "fetch", "-q", "--depth", "1", repository, revision])
    run(["git", "-C", str(root), "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD"])


def compile_sources(otp: Path, workspace: Path, sources: Sources) -> Checkouts:
    """Compile the pinned sources at a fixed absolute path with a fixed build date.

    Elixir keeps absolute source paths in quoted literals and System embeds its build date,
    so the BEAM bytes are reproducible only when both are fixed.
    """
    if workspace.is_symlink() or (workspace.exists() and workspace.stat().st_uid != os.getuid()):
        raise ValueError("The GenStage workspace must be a directory owned by this user")
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(mode=0o700, parents=True)
    elixir = workspace / "elixir"
    genstage = workspace / "gen_stage"
    bridge = workspace / "bridge"
    checkout(elixir, REPOSITORIES["elixir"], sources.elixir)
    checkout(genstage, REPOSITORIES["gen_stage"], sources.gen_stage)
    environment = os.environ | {
        "ERL_COMPILER_OPTIONS": "deterministic",
        "SOURCE_DATE_EPOCH": run(["git", "-C", str(elixir), "log", "-1", "--format=%ct"]).strip(),
        "MIX_HOME": str(workspace / "mix"),
        "HEX_HOME": str(workspace / "hex"),
        "PATH": os.pathsep.join((str(elixir / "bin"), str(otp / "bin"), os.environ["PATH"])),
    }

    def build(arguments: list[str], cwd: Path) -> None:
        subprocess.run(arguments, cwd=cwd, env=environment, check=True)  # noqa: S603

    build(["make", "-j" + str(os.cpu_count() or 1), "compile"], elixir)
    build(["mix", "local.hex", HEX_VERSION, "--force"], genstage)
    build(["mix", "compile", "--no-deps-check"], genstage)
    ebin = bridge / "ebin"
    ebin.mkdir(parents=True)
    for name in ("pybeam_genstage.erl", "graph.ex", "pybeam_genstage.app"):
        shutil.copyfile(BRIDGE / name, bridge / name)
    shutil.copyfile(bridge / "pybeam_genstage.app", ebin / "pybeam_genstage.app")
    build(["erlc", "+deterministic", "-o", "ebin", "pybeam_genstage.erl"], bridge)
    build(
        [
            "elixirc",
            "--ignore-module-conflict",
            "-pa",
            str(genstage / "_build/dev/lib/gen_stage/ebin"),
            "-o",
            "ebin",
            "graph.ex",
        ],
        bridge,
    )
    return Checkouts(elixir, genstage, ebin)


def build_manifest(root: Path, otp: Path, erts: str, sources: Sources) -> OptionalManifest:
    paths = [str(path.resolve()) for path in sorted(root.glob("lib/*/ebin"))]
    expression = (
        'Apps=[begin [AppFile]=filelib:wildcard(filename:join(Dir,"*.app")), '
        "{ok,[{application,Name,Props}]}=file:consult(AppFile), "
        "Version=proplists:get_value(vsn,Props), "
        'Modules=[begin {ok,B}=file:read_file(filename:join(Dir,atom_to_list(M)++".beam")), '
        "{ok,{M,D}}=beam_lib:md5(B), #{name=>atom_to_binary(M),md5=>binary:encode_hex(D,lowercase)} "
        "end || M<-proplists:get_value(modules,Props)], "
        "#{name=>atom_to_binary(Name),version=>list_to_binary(Version), "
        'code_path=>list_to_binary(filename:join(["lib",atom_to_list(Name)++"-"++Version,"ebin"])), '
        "dependencies=>[atom_to_binary(A)||A<-proplists:get_value(applications,Props,[])], "
        "modules=>Modules} end || Dir<-" + json.dumps(paths) + "], "
        "io:put_chars(json:encode(Apps)),halt()."
    )
    applications = TypeAdapter(tuple[ApplicationAsset, ...]).validate_json(
        run([str(otp / "bin/erl"), "+S", "2:2", "-noshell", "-eval", expression])
    )
    files = tuple(
        AssetFile(path=path.relative_to(root), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    )
    return OptionalManifest(
        format_version=1,
        name="genstage",
        otp_version=(otp / "releases/29/OTP_VERSION").read_text().strip(),
        erts_version=erts,
        sources=sources,
        applications=applications,
        files=files,
    )


def stage_genstage(otp: Path, runtime: Path, workspace: Path, derived: Path | None) -> None:
    """Build and stage the payload, writing the built manifest to `derived` when it differs."""
    approved = approved_manifest("genstage")
    checkouts = compile_sources(otp, workspace, approved.sources)
    elixir, genstage, bridge = checkouts.elixir, checkouts.genstage, checkouts.bridge
    pinned(elixir, approved.sources.elixir)
    pinned(genstage, approved.sources.gen_stage)
    (compiler,) = (otp / "lib").glob("compiler-*")
    output = runtime / "optional/genstage"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        root = Path(temporary)
        sources = {
            "compiler": compiler / "ebin",
            "elixir": elixir / "lib/elixir/ebin",
            "logger": elixir / "lib/logger/ebin",
            "gen_stage": genstage / "_build/dev/lib/gen_stage/ebin",
            "pybeam_genstage": bridge,
        }
        for app in approved.applications:
            source = sources[app.name]
            app_text = (source / (app.name + ".app")).read_text()
            match = re.search(r'\{vsn,\s*"([0-9.]+)"\}', app_text)
            if match is None or match[1] != app.version:
                raise ValueError("Unexpected optional application version: " + app.name)
            target = root / app.code_path
            target.mkdir(parents=True)
            for path in source.iterdir():
                if path.suffix in {".app", ".beam"} and path.is_file():
                    shutil.copyfile(path, target / path.name)
        expression = (
            "lists:foreach(fun(File) -> {ok, B}=file:read_file(File), "
            "{ok,{_,S}}=beam_lib:strip(B), ok=file:write_file(File,S) end, "
            "filelib:wildcard(" + json.dumps(str(root / "lib/*/ebin/*.beam")) + ")), halt()."
        )
        run([str(otp / "bin/erl"), "+S", "2:2", "-noshell", "-eval", expression])
        notices = root / "notices"
        notices.mkdir()
        shutil.copyfile(elixir / "LICENSE", notices / "Elixir-LICENSE")
        shutil.copytree(elixir / "LICENSES", notices / "Elixir-LICENSES")
        shutil.copyfile(genstage / "README.md", notices / "GenStage-NOTICE.md")
        shutil.copyfile(runtime / "notices/OTP-LICENSE.txt", notices / "OTP-LICENSE.txt")
        core = RuntimeManifest.model_validate_json((runtime / "runtime.json").read_bytes()).root
        manifest = build_manifest(root, otp, core.erts_version, approved.sources)
        if manifest != approved:
            if derived is not None:
                derived.write_text(manifest.model_dump_json(indent=2) + "\n")
            raise ValueError("Built optional assets differ from the approved pinned specification")
        write_manifest(root, manifest)
        verify_manifest(root)
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(root, output)


class Arguments(argparse.Namespace):
    otp: Path
    runtime: Path
    workspace: Path
    derived: Path | None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build and stage pinned optional BEAM applications"
    )
    parser.add_argument("--otp", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, default=Path("build/runtime"))
    parser.add_argument("--workspace", type=Path, default=WORKSPACE)
    parser.add_argument(
        "--derived", type=Path, help="Write the built manifest here when it differs"
    )
    args = parser.parse_args(namespace=Arguments())
    stage_genstage(args.otp, args.runtime, args.workspace, args.derived)


if __name__ == "__main__":
    main()
