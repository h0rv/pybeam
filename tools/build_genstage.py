import argparse
import hashlib
import json
import re
import shutil
import tempfile
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


def pinned(root: Path, revision: str) -> None:
    actual = run(["git", "-C", str(root), "rev-parse", "HEAD"]).strip()
    if actual != revision:
        raise ValueError("Unexpected optional dependency revision: " + str(root))
    run(["git", "-C", str(root), "diff", "--quiet", "HEAD"])


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


def stage_genstage(otp: Path, elixir: Path, genstage: Path, bridge: Path, runtime: Path) -> None:
    approved = approved_manifest("genstage")
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
            raise ValueError("Built optional assets differ from the approved pinned specification")
        write_manifest(root, manifest)
        verify_manifest(root)
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(root, output)


class Arguments(argparse.Namespace):
    otp: Path
    elixir: Path
    genstage: Path
    bridge: Path
    runtime: Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage pinned optional BEAM applications")
    for name in ("otp", "elixir", "genstage", "bridge", "runtime"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(namespace=Arguments())
    stage_genstage(args.otp, args.elixir, args.genstage, args.bridge, args.runtime)


if __name__ == "__main__":
    main()
