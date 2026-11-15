import argparse
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from tools.build_runtime import run

ELIXIR = "853b7cd719bce7df87be4271ee91c90acdf706e1"
GENSTAGE = "d1532fa56482bc92d90abc777439ff8be34b5b1a"


def pinned(root: Path, revision: str) -> None:
    actual = run(["git", "-C", str(root), "rev-parse", "HEAD"]).strip()
    if actual != revision:
        raise ValueError("Unexpected optional dependency revision: " + str(root))
    run(["git", "-C", str(root), "diff", "--quiet", "HEAD"])


def stage_genstage(otp: Path, elixir: Path, genstage: Path, bridge: Path, runtime: Path) -> None:
    pinned(elixir, ELIXIR)
    pinned(genstage, GENSTAGE)
    (compiler,) = (otp / "lib").glob("compiler-*")
    output = runtime / "optional/genstage"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        root = Path(temporary)
        apps: list[dict[str, str]] = []
        for name, source, expected in (
            ("compiler", compiler / "ebin", None),
            ("elixir", elixir / "lib/elixir/ebin", "1.20.0"),
            ("logger", elixir / "lib/logger/ebin", "1.20.0"),
            ("gen_stage", genstage / "_build/dev/lib/gen_stage/ebin", "1.3.2"),
            ("pybeam_genstage", bridge, "0.1.0"),
        ):
            app_text = (source / (name + ".app")).read_text()
            match = re.search(r'\{vsn,\s*"([0-9.]+)"\}', app_text)
            if match is None or (expected is not None and match[1] != expected):
                raise ValueError("Unexpected optional application version: " + name)
            destination = Path("lib") / (name + "-" + match[1]) / "ebin"
            target = root / destination
            target.mkdir(parents=True)
            for path in source.iterdir():
                if path.suffix in {".app", ".beam"} and path.is_file():
                    shutil.copyfile(path, target / path.name)
            apps.append({"name": name, "version": match[1], "code_path": str(destination)})
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
        runtime_data: object = json.loads((runtime / "runtime.json").read_bytes())
        if not isinstance(runtime_data, dict):
            raise ValueError("Invalid core runtime manifest")
        erts: object = runtime_data.get("erts_version")
        if not isinstance(erts, str):
            raise ValueError("Missing core ERTS version")
        otp_version = (otp / "releases/29/OTP_VERSION").read_text().strip()
        if otp_version != "29.1.2":
            raise ValueError("Optional assets require OTP 29.1.2")
        files = [
            {
                "path": str(path.relative_to(root)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]
        manifest = {
            "name": "genstage",
            "otp_version": otp_version,
            "erts_version": erts,
            "sources": {"elixir": ELIXIR, "gen_stage": GENSTAGE},
            "applications": apps,
            "files": files,
        }
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
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
