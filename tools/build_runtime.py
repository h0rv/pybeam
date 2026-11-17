import argparse
import json
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class OTPVersions:
    erts: str
    kernel: str
    stdlib: str
    crypto: str


@dataclass(frozen=True, slots=True)
class MachO:
    dependencies: tuple[str, ...]
    rpaths: tuple[str, ...]
    minimum: tuple[int, int]


def run(arguments: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.check_output(arguments, cwd=cwd, text=True)  # noqa: S603


MACHO_MAGICS = frozenset(
    bytes.fromhex(value)
    for value in (
        "feedface",
        "cefaedfe",
        "feedfacf",
        "cffaedfe",
        "cafebabe",
        "bebafeca",
        "cafebabf",
        "bfbafeca",
    )
)
FAT_MAGICS = frozenset(
    bytes.fromhex(value) for value in ("cafebabe", "bebafeca", "cafebabf", "bfbafeca")
)
ERTS_EXECUTABLES = ("beam.smp", "erlexec", "erl_child_setup", "inet_gethost", "epmd")
ELF_MAGIC = b"\x7fELF"
LINUX_LIBRARIES = frozenset(
    {
        "libc.so.6",
        "libm.so.6",
        "libstdc++.so.6",
        "libgcc_s.so.1",
        "libtinfo.so.6",
        "libz.so.1",
        "libcrypto.so.3",
        "ld-linux-x86-64.so.2",
        "libpthread.so.0",
        "libdl.so.2",
        "librt.so.1",
        "libutil.so.1",
    }
)
MANYLINUX_LIBRARIES = LINUX_LIBRARIES - {"libtinfo.so.6", "libcrypto.so.3"}
MANYLINUX_FLOOR = (2, 17)
# Oldest manylinux glibc level whose libstdc++ and libgcc_s provide each symbol version,
# from auditwheel's manylinux policy.
GLIBCXX_LEVELS = (
    ((3, 4, 19), (2, 17)),
    ((3, 4, 22), (2, 24)),
    ((3, 4, 24), (2, 27)),
    ((3, 4, 28), (2, 31)),
    ((3, 4, 29), (2, 34)),
    ((3, 4, 30), (2, 35)),
    ((3, 4, 33), (2, 39)),
)
LIBGCC_LEVELS = (
    ((4, 8, 0), (2, 17)),
    ((7, 0, 0), (2, 27)),
    ((12, 0, 0), (2, 35)),
    ((14, 0, 0), (2, 39)),
)
CXXABI_LEVELS = (
    ((1, 3, 7), (2, 17)),
    ((1, 3, 10), (2, 24)),
    ((1, 3, 11), (2, 27)),
    ((1, 3, 12), (2, 31)),
    ((1, 3, 13), (2, 34)),
    ((1, 3, 14), (2, 39)),
)


@dataclass(frozen=True, slots=True)
class ELF:
    dependencies: tuple[str, ...]
    rpaths: tuple[str, ...]
    manylinux: tuple[int, int] | None = MANYLINUX_FLOOR
    """Oldest manylinux glibc level that provides every symbol version, or None if none does."""


def audit_elf(path: Path, *, python_nif: bool = False) -> ELF:
    if macho_magic(path) != ELF_MAGIC:
        raise ValueError("Required native artifact is not ELF: " + str(path))
    header = run(["readelf", "-h", str(path)])
    if not re.search(r"Machine:\s+Advanced Micro Devices X86-64\s*$", header, re.MULTILINE):
        raise ValueError("Only x86-64 ELF artifacts are supported: " + str(path))
    dynamic = run(["readelf", "-d", str(path)])
    dependencies = tuple(re.findall(r"\(NEEDED\).*?\[(.*?)\]", dynamic))
    rpaths = tuple(
        entry
        for value in re.findall(r"\((?:RUNPATH|RPATH)\).*?\[(.*?)\]", dynamic)
        for entry in value.split(":")
    )
    if any(
        (entry != "$ORIGIN" and not entry.startswith("$ORIGIN/")) or ".." in Path(entry).parts
        for entry in rpaths
    ):
        raise ValueError("Runtime contains a nonrelocatable ELF search path: " + str(path))
    if python_nif and dependencies.count("libpython3.14.so.1.0") != 1:
        raise ValueError("Native runtime must link conventional CPython 3.14 exactly once")
    allowed = LINUX_LIBRARIES | ({"libpython3.14.so.1.0"} if python_nif else set())
    for dependency in dependencies:
        if dependency not in allowed:
            raise ValueError("Unbundled shared library: " + dependency + " in " + str(path))
    return ELF(dependencies, rpaths, manylinux_level(run(["readelf", "-V", "--wide", str(path)])))


def manylinux_level(versions: str) -> tuple[int, int] | None:
    glibc = [
        (int(major), int(minor)) for major, minor in re.findall(r"\bGLIBC_(\d+)\.(\d+)\b", versions)
    ]
    levels = [MANYLINUX_FLOOR, *glibc]
    for prefix, table in (
        ("GLIBCXX", GLIBCXX_LEVELS),
        ("CXXABI", CXXABI_LEVELS),
        ("GCC", LIBGCC_LEVELS),
    ):
        for match in re.findall(r"\b" + prefix + r"_(\d+(?:\.\d+)*)\b", versions):
            version = tuple(int(part) for part in match.split("."))
            level = next((level for provided, level in table if version <= provided), None)
            if level is None:
                return None
            levels.append(level)
    return max(levels)


def linux_wheel_tag(binaries: tuple[ELF, ...]) -> str:
    """Claim manylinux only when every binary needs nothing beyond a manylinux level's libraries.

    The Python NIF's libpython dependency is resolved from the installing interpreter.
    """
    portable = MANYLINUX_LIBRARIES | {"libpython3.14.so.1.0"}
    levels = [binary.manylinux for binary in binaries]
    if None in levels or any(not portable.issuperset(binary.dependencies) for binary in binaries):
        return "cp314-cp314-linux_x86_64"
    major, minor = max(level for level in levels if level is not None)
    return "cp314-cp314-manylinux_{}_{}_x86_64".format(major, minor)


def macho_magic(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(4)


def native_binaries(root: Path, required: tuple[Path, ...]) -> tuple[Path, ...]:
    discovered = tuple(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path not in required
        and macho_magic(path) in MACHO_MAGICS | {ELF_MAGIC}
    )
    return (*required, *discovered)


def inspect_macho(path: Path) -> MachO:
    magic = macho_magic(path)
    if magic in FAT_MAGICS:
        raise ValueError(
            "Universal Mach-O containers are unsupported; require thin arm64: " + str(path)
        )
    if magic not in MACHO_MAGICS:
        raise ValueError("Required native artifact is not Mach-O: " + str(path))
    architecture = run(["/usr/bin/lipo", "-archs", str(path)]).strip()
    if architecture != "arm64":
        raise ValueError("Only thin arm64 artifacts are supported: " + str(path))
    dependencies = tuple(
        line.strip().split(" (", 1)[0]
        for line in run(["/usr/bin/otool", "-L", str(path)]).splitlines()[1:]
    )
    commands = run(["/usr/bin/otool", "-l", str(path)])
    builds = re.findall(
        r"cmd LC_BUILD_VERSION\s+cmdsize \d+\s+platform (\S+)\s+minos (\d+)\.(\d+)", commands
    )
    if any(apple_platform != "1" for apple_platform, _, _ in builds) or re.search(
        r"cmd LC_VERSION_MIN_(?!MACOSX\b)", commands
    ):
        raise ValueError("Native artifacts must target PLATFORM_MACOS (1): " + str(path))
    versions = [(major, minor) for _, major, minor in builds]
    versions.extend(
        re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+)\.(\d+)", commands)
    )
    if not versions:
        raise ValueError("Missing macOS deployment target: " + str(path))
    minimum = max((int(major), int(minor)) for major, minor in versions)
    rpaths = tuple(re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset", commands))
    return MachO(dependencies, rpaths, minimum)


def wheel_tag(minimum: tuple[int, int]) -> str:
    if minimum[0] >= 11 and minimum[1] != 0:
        raise ValueError(
            "macOS wheel installers use major.0 compatibility tags; rebuild native artifacts "
            "with MACOSX_DEPLOYMENT_TARGET set to a supported major.0 floor"
        )
    return "cp314-cp314-macosx_{}_{}_arm64".format(*minimum)


def system_library(path: str) -> bool:
    return path.startswith(("/usr/lib/", "/System/Library/"))


def audit_binary(path: Path, *, python_nif: bool = False) -> MachO:
    info = inspect_macho(path)
    if python_nif and info.dependencies.count("@rpath/libpython3.14.dylib") != 1:
        raise ValueError("Native runtime must link conventional CPython 3.14 exactly once")
    for dependency in info.dependencies:
        if system_library(dependency):
            continue
        if python_nif and dependency == "@rpath/libpython3.14.dylib":
            continue
        raise ValueError("Unbundled shared library: " + dependency + " in " + str(path))
    return info


def otp_information(erl: Path) -> OTPVersions:
    expression = (
        "application:load(crypto), "
        "io:put_chars(json:encode(#{erts => list_to_binary(erlang:system_info(version)), "
        "kernel => list_to_binary(element(2, application:get_key(kernel, vsn))), "
        "stdlib => list_to_binary(element(2, application:get_key(stdlib, vsn))), "
        "crypto => list_to_binary(element(2, application:get_key(crypto, vsn)))})), halt()."
    )
    raw: object = json.loads(run([str(erl), "+S", "1:1", "-noshell", "-eval", expression]))
    if not isinstance(raw, dict):
        raise ValueError("Invalid OTP build information")

    def version(key: str) -> str:
        value: object = raw.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9.]+", value):
            raise ValueError("Invalid OTP version: " + key)
        return value

    return OTPVersions(version("erts"), version("kernel"), version("stdlib"), version("crypto"))


def erlang_string(path: Path) -> str:
    return json.dumps(str(path))


def stage_hornbeam(app: Path, license_file: Path, output: Path) -> Path:
    specification = (app / "ebin" / "hornbeam.app").read_text()
    version = re.search(r'\{vsn,\s*"([0-9.]+)"\}', specification)
    if version is None:
        raise ValueError("Cannot determine Hornbeam application version")
    if not license_file.is_file():
        raise ValueError("Required Hornbeam license missing")
    code_path = Path("lib", "hornbeam-" + version.group(1), "ebin")
    destination = output / code_path.parent
    for directory in ("ebin", "priv"):
        shutil.copytree(app / directory, destination / directory)
    for cache in destination.rglob("__pycache__"):
        shutil.rmtree(cache)
    (output / "notices").mkdir(exist_ok=True)
    shutil.copy2(license_file, output / "notices" / "hornbeam-LICENSE")
    return code_path


def stage_runtime(
    otp: Path,
    native: Path,
    otp_license: Path,
    native_license: Path,
    output: Path,
    *,
    hornbeam: Path | None = None,
    hornbeam_license: Path | None = None,
) -> None:
    if (hornbeam is None) != (hornbeam_license is None):
        raise ValueError("--hornbeam-app and --hornbeam-license must be supplied together")
    otp = otp.resolve()
    native = native.resolve()
    otp_license = otp_license.resolve()
    native_license = native_license.resolve()
    output = output.resolve()
    linux = platform.system() == "Linux" and platform.machine() == "x86_64"
    if not linux and (platform.system() != "Darwin" or platform.machine() != "arm64"):
        raise ValueError("Runtime staging supports macOS arm64 and Linux x86_64 only")
    for license_file in (otp_license, native_license):
        if not license_file.is_file():
            raise ValueError("Required third-party license missing: " + str(license_file))
    erl = otp / "bin" / "erl"
    versions = otp_information(erl)
    app_text = (native / "ebin" / "erlang_python.app").read_text()
    version_match = re.search(r'\{vsn,\s*"([0-9.]+)"\}', app_text)
    if version_match is None:
        raise ValueError("Cannot determine erlang_python application version")
    native_version = version_match[1]
    if not (native / "ebin" / "py_launch.beam").is_file():
        raise ValueError("Canonical native runtime needs a rebuilt py_launch.beam")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-", dir=output.parent) as temporary:
        work = Path(temporary)
        release = work / "pybeam"
        release.with_suffix(".rel").write_text(
            '{{release,{{"pybeam","0.1.0"}},{{erts,"{}"}},'
            '[{{kernel,"{}"}},{{stdlib,"{}"}},{{crypto,"{}"}},{{erlang_python,"{}",load}}]}}.\n'.format(
                versions.erts, versions.kernel, versions.stdlib, versions.crypto, native_version
            )
        )
        expression = (
            "ok = systools:make_script({release}, [{{path,[{ebin}]}},no_warn_sasl]), "
            "ok = systools:make_tar({release}, [{{path,[{ebin}]}},"
            "{{erts,{otp}}},no_warn_sasl]), halt()."
        ).format(
            release=erlang_string(release),
            ebin=erlang_string(native / "ebin"),
            otp=erlang_string(otp),
        )
        run([str(erl), "+S", "1:1", "-noshell", "-eval", expression], cwd=work)
        staged = work / "staged"
        staged.mkdir()
        with tarfile.open(release.with_suffix(".tar.gz")) as archive:
            archive.extractall(staged, filter="data")
        erts_bin = staged / ("erts-" + versions.erts) / "bin"
        keep = set(ERTS_EXECUTABLES)
        for item in erts_bin.iterdir():
            if item.name not in keep:
                item.unlink()
        bin_dir = staged / "bin"
        bin_dir.mkdir(exist_ok=True)
        template = (otp / ("erts-" + versions.erts) / "bin" / "erl.src").read_text()
        (bin_dir / "erl").write_text(template.replace("%FINAL_ROOTDIR%", "/"))
        (bin_dir / "erl").chmod(0o755)
        boot = staged / "releases" / "0.1.0" / "start.boot"
        shutil.copy2(boot, bin_dir / "start.boot")
        shutil.copy2(boot, bin_dir / "start_clean.boot")
        for version_file in otp.glob("releases/*/OTP_VERSION"):
            destination = staged / version_file.relative_to(otp)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(version_file, destination)
        app = staged / "lib" / ("erlang_python-" + native_version)
        priv = app / "priv"
        for item in priv.iterdir():
            if item.name not in {
                "py_nif.so",
                "_erlang_impl",
                "_erlang_isolated",
                "erlang-stubs",
                "erlang_services.py",
                "py_isolated_child.py",
            }:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        for cache in staged.rglob("__pycache__"):
            shutil.rmtree(cache)
        code_paths = [
            str(app.relative_to(staged) / "ebin"),
            "lib/crypto-" + versions.crypto + "/ebin",
        ]
        if hornbeam is not None and hornbeam_license is not None:
            code_paths.append(str(stage_hornbeam(hornbeam, hornbeam_license, staged)))
        nif = priv / "py_nif.so"
        required = (nif, *(erts_bin / name for name in ERTS_EXECUTABLES))
        minimum = (0, 0)
        elves: list[ELF] = []
        if linux:
            for item in native_binaries(staged, required):
                run(["patchelf", "--remove-rpath", str(item)])
                elves.append(audit_elf(item, python_nif=item == nif))
        else:
            nif_info = audit_binary(nif, python_nif=True)
            for rpath in nif_info.rpaths:
                run(["/usr/bin/install_name_tool", "-delete_rpath", rpath, str(nif)])
            run(["/usr/bin/install_name_tool", "-add_rpath", "@loader_path", str(nif)])
            run(["/usr/bin/codesign", "--force", "--sign", "-", str(nif)])
            minimum = max(
                audit_binary(item, python_nif=item == nif).minimum
                for item in native_binaries(staged, required)
            )
        notices = staged / "notices"
        notices.mkdir(exist_ok=True)
        shutil.copy2(otp_license, notices / "OTP-LICENSE.txt")
        shutil.copy2(native_license, notices / "erlang-python-LICENSE")
        for source, destination in ((otp, notices / "otp"), (native, notices / "erlang-python")):
            for name in ("COPYRIGHT", "NOTICE", "NOTICE.txt", "LICENSE", "LICENSE.txt"):
                candidate = source / name
                if candidate.is_file():
                    destination.mkdir(exist_ok=True)
                    shutil.copy2(candidate, destination / name)
        manifest = {
            "python_version": "3.14",
            "implementation": "cpython",
            "architecture": "arm64",
            "system": "Darwin",
            "minimum_macos": list(minimum),
            "wheel_tag": wheel_tag(minimum),
            "executable": "erts-" + versions.erts + "/bin/erlexec",
            "root_dir": ".",
            "bindir": "erts-" + versions.erts + "/bin",
            "code_paths": code_paths,
            "erts_version": versions.erts,
            "python_library": "libpython3.14.dylib",
        }
        if linux:
            manifest.update(
                architecture="x86_64",
                system="Linux",
                wheel_tag=linux_wheel_tag(tuple(elves)),
                python_library="libpython3.14.so.1.0",
            )
            del manifest["minimum_macos"]
        (staged / "runtime.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if output.exists():
            shutil.rmtree(output)
        shutil.move(str(staged), output)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage an already-built native runtime for a wheel"
    )
    parser.add_argument("--otp-root", type=Path, required=True)
    parser.add_argument("--native-app", type=Path, required=True)
    parser.add_argument("--otp-license", type=Path, required=True)
    parser.add_argument("--native-license", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("build/runtime"))
    parser.add_argument("--hornbeam-app", type=Path)
    parser.add_argument("--hornbeam-license", type=Path)
    args = parser.parse_args()
    stage_runtime(
        args.otp_root,
        args.native_app,
        args.otp_license,
        args.native_license,
        args.output,
        hornbeam=args.hornbeam_app,
        hornbeam_license=args.hornbeam_license,
    )


if __name__ == "__main__":
    main()
