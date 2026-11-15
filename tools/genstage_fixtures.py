import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from tools.genstage_assets import stage

SOURCES = Path(__file__).resolve().parents[1] / "tests/native"
FIXTURES = (
    "py_application_probe.app",
    "py_application_probe.erl",
    "Elixir.GenStage.erl",
    "pybeam_genstage_probe.app",
    "pybeam_genstage_probe.erl",
    "genstage_probe.ex",
)


def prepare(wheel: Path, output: Path) -> None:
    """Compile tracked test fixtures into a new ephemeral directory, never the source tree."""
    if output.exists():
        raise FileExistsError("Test fixtures require a new output directory")
    for name in FIXTURES:
        if not (SOURCES / name).is_file():
            raise FileNotFoundError("Tracked test fixture is missing: " + name)
    erl, erlc = shutil.which("erl"), shutil.which("erlc")
    if erl is None or erlc is None:
        raise RuntimeError("Explicit native fixture tests require OTP 29.1.2 erl and erlc")
    runtime = stage(wheel, output / "assets")
    environment = dict(os.environ, ERL_FLAGS="+S 2:2", ERL_ZFLAGS="+S 2:2")
    for app, destination in (
        ("py_application_probe", output / "compat/ebin"),
        ("pybeam_genstage_probe", output / "probe/ebin"),
    ):
        destination.mkdir(parents=True)
        shutil.copyfile(SOURCES / (app + ".app"), destination / (app + ".app"))
        subprocess.run(  # noqa: S603
            [erlc, "-o", str(destination), str(SOURCES / (app + ".erl"))],
            env=environment,
            check=True,
            timeout=30,
        )
    stale = output / "stale"
    stale.mkdir()
    subprocess.run(  # noqa: S603
        [erlc, "-o", str(stale), str(SOURCES / "Elixir.GenStage.erl")],
        env=environment,
        check=True,
        timeout=30,
    )
    (output / "compat/priv").mkdir()
    shutil.copyfile(stale / "Elixir.GenStage.beam", output / "compat/priv/stale.beam")
    paths = [str(path) for path in sorted((runtime / "optional/genstage/lib").glob("*/ebin"))]
    expression = (
        '"29.1.2" = string:trim(binary_to_list(element(2,file:read_file(filename:join([code:root_dir(),'
        '"releases","29","OTP_VERSION"]))))), '
        "ok=code:add_pathsa(" + json.dumps(paths) + "), "
        "{ok,_}=application:ensure_all_started(elixir), "
        "Results='Elixir.Code':compile_file(list_to_binary("
        + json.dumps(str(SOURCES / "genstage_probe.ex"))
        + ")), lists:foreach(fun({M,B})->ok=file:write_file(filename:join("
        + json.dumps(str(output / "probe/ebin"))
        + ',atom_to_list(M)++".beam"),B) end,Results), halt().'
    )
    subprocess.run(  # noqa: S603
        [erl, "+S", "2:2", "-noshell", "-eval", expression],
        env=environment,
        check=True,
        timeout=30,
    )


class Arguments(argparse.Namespace):
    wheel: Path
    output: Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile tracked installed-wheel fixtures only")
    parser.add_argument("wheel", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(namespace=Arguments())
    prepare(args.wheel.resolve(strict=True), args.output.absolute())


if __name__ == "__main__":
    main()
