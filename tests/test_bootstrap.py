import asyncio
import inspect
import json
import os
import py_compile
import signal
import subprocess
import sys
import sysconfig
import time
import zipfile
from pathlib import Path
from types import FunctionType

import pytest

from pybeam.runtime import loader, process
from pybeam.runtime.assets import RuntimeAssets, development_assets, packaged_assets
from pybeam.runtime.config import BootstrapError, CleanupError, Config, load_config, parse_config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Config()


@pytest.fixture(autouse=True)
def preserve_main_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])


def test_loader__entry_classes_keep_source_after_loading(tmp_path: Path) -> None:
    entry = tmp_path / "entry.py"
    source = "class View:\n    pass\n"
    entry.write_text(source)
    namespace = loader._run_entry(entry)
    view = namespace["View"]
    assert isinstance(view, type)
    assert inspect.getfile(view) == str(entry)
    assert inspect.getsource(view) == source
    assert vars(sys.modules["__main__"]) is namespace
    assert namespace["__spec__"] is None


def test_loader__failed_entry_restores_main_module(tmp_path: Path) -> None:
    previous = sys.modules["__main__"]
    entry = tmp_path / "entry.py"
    entry.write_text("raise ValueError('load failed')\n")
    with pytest.raises(ValueError, match="load failed"):
        loader._run_entry(entry)
    assert sys.modules["__main__"] is previous


def test_import__does_not_load_erlang() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import pybeam, sys; assert 'erlang' not in sys.modules"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "value",
    [
        {"workers": True},
        {"workers": 2},
        {"distribution": True},
        {"listeners": "off"},
        {"mode": "attach"},
        {"mode": "external"},
        {"mode": "custom"},
        {"startup_timeout": 0},
        {"shutdown_timeout": float("inf")},
        {"startup_timeout": True},
        {"development_runtime": 1},
        {"launch_command": ["erl"]},
    ],
)
def test_config__rejects_unsupported_or_invalid_settings(value: object, tmp_path: Path) -> None:
    with pytest.raises(BootstrapError):
        parse_config(value, tmp_path)


def test_config__defaults_and_relative_development_path(tmp_path: Path) -> None:
    assert parse_config({}, tmp_path) == Config(data_dir=tmp_path / ".pybeam/data")
    project = tmp_path / "pyproject.toml"
    project.write_text('[tool.pybeam]\ndevelopment_runtime = "build"\nstartup_timeout = 2.5\n')
    child = tmp_path / "nested"
    child.mkdir()
    config = load_config(child)
    assert config.development_runtime == tmp_path / "build"
    assert config.startup_timeout == 2.5


def test_packaged_assets__rejects_wrong_python_and_path_escape(tmp_path: Path) -> None:
    manifest = tmp_path / "runtime.json"
    manifest.write_text(json.dumps({"python_version": "0.0"}))
    with pytest.raises(BootstrapError, match="not compatible"):
        packaged_assets(tmp_path)
    manifest.write_text(
        json.dumps(
            {
                "python_version": "{}.{}".format(sys.version_info.major, sys.version_info.minor),
                "implementation": "cpython",
                "architecture": "arm64",
                "system": "Darwin",
                "minimum_macos": [11, 0],
                "wheel_tag": "cp314-cp314-macosx_11_0_arm64",
                "executable": "../erl",
                "root_dir": ".",
                "bindir": "erts-1/bin",
                "erts_version": "1",
                "python_library": "libpython3.14.dylib",
                "code_paths": ["ebin"],
            }
        )
    )
    with pytest.raises(BootstrapError, match="inside"):
        packaged_assets(tmp_path)


def test_development_assets__rejects_incompatible_nif_without_loading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("pybeam.runtime.assets.shutil.which", lambda _name: sys.executable)
    app = tmp_path / "lib" / "erlang_python"
    ebin = app / "ebin"
    ebin.mkdir(parents=True)
    (ebin / "erlang_python.app").touch()
    (ebin / "py_launch.beam").touch()
    (app / "priv").mkdir()
    (app / "priv" / "py_nif.so").write_bytes(b"libpython0.0.dylib")
    with pytest.raises(BootstrapError, match=r"Cannot verify.*compatibility"):
        development_assets(tmp_path)


@pytest.fixture
def fake_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "vm.py"

    def command(
        _assets: RuntimeAssets,
        *,
        python_paths: tuple[str, ...] = (),
        startup: tuple[str, ...] = (),
    ) -> list[str]:
        return [sys.executable, str(script)]

    monkeypatch.setattr(process, "_command", command)
    return script


def _launch(script: Path, config: Config = DEFAULT_CONFIG) -> int:
    return process.launch(script, ["a", "--flag"], config, RuntimeAssets(Path(sys.executable), ()))


def test_process__preserves_args_environment_cwd_stdio(
    fake_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PYBEAM_TEST_VALUE", "retained")
    monkeypatch.chdir(fake_runtime.parent)
    fake_runtime.write_text(
        "import json, os\nfrom pathlib import Path\n"
        "p = json.loads(os.environ['PYBEAM_BOOTSTRAP'])\n"
        "Path(p['ready']).touch()\n"
        "pending = Path(p['completed']).with_suffix('.tmp')\n"
        "pending.write_text('0')\n"
        "os.replace(pending, p['completed'])\n"
        "pending = Path(os.environ['PYBEAM_CLEANUP'] + '.tmp')\n"
        "pending.write_text('clean')\n"
        "pending.replace(os.environ['PYBEAM_CLEANUP'])\n"
        "print(json.dumps([p['argv'], os.getcwd(), os.environ['PYBEAM_TEST_VALUE']]))\n"
    )
    assert _launch(fake_runtime) == 0
    output = json.loads(capfd.readouterr().out)
    assert output == [[str(fake_runtime), "a", "--flag"], str(fake_runtime.parent), "retained"]


@pytest.mark.parametrize("boot", [None, Path("runtime/bin/start")])
def test_process__command_uses_packaged_boot_without_a_script_launcher(boot: Path | None) -> None:
    assets = RuntimeAssets(
        Path("runtime/erts/bin/erlexec"), (Path("runtime/lib/app/ebin"),), boot=boot
    )
    command = process._command(assets)
    assert command[0] == str(assets.executable)
    assert "+S" not in command
    assert command[-3:] == ["-s", "py_launch", "start"]
    if boot is None:
        assert "-boot" not in command
    else:
        assert command[command.index("-boot") + 1] == str(boot)


def test_process__command_registers_utf8_python_paths_as_data() -> None:
    assets = RuntimeAssets(Path("erl"), ())
    command = process._command(assets, python_paths=('a"b', "é"))
    position = command.index("-erlang_python")
    assert command[position : position + 3] == [
        "-erlang_python",
        "paths",
        "[<<97,34,98>>,<<195,169>>]",
    ]
    assert "paths" not in process._command(assets)


@pytest.mark.skipif(os.name != "posix", reason="POSIX surrogate-escaped filesystem paths")
def test_process__command_selects_caller_python_for_isolated_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "executable", os.fsdecode(b'/python-\xff/"python'))
    assets = RuntimeAssets(Path("erl"), ())
    command = process._command(assets)
    position = command.index("isolated_python")
    assert command[position - 1 : position + 2] == [
        "-erlang_python",
        "isolated_python",
        "<<47,112,121,116,104,111,110,45,255,47,34,112,121,116,104,111,110>>",
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX surrogate-escaped filesystem paths")
def test_process__command_registers_surrogate_escaped_paths_as_bytes() -> None:
    assets = RuntimeAssets(Path("erl"), ())
    command = process._command(assets, python_paths=("", os.fsdecode(b"entry-\xff")))
    position = command.index("-erlang_python")
    assert command[position : position + 3] == [
        "-erlang_python",
        "paths",
        "[<<>>,<<101,110,116,114,121,45,255>>]",
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX surrogate-escaped filesystem paths")
def test_process__bootstrap_roundtrips_surrogate_escaped_paths(
    fake_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    entry = fake_runtime.parent / os.fsdecode(b"entry-\xff") / "main.py"
    search = str(fake_runtime.parent / os.fsdecode(b"search-\xfe"))
    monkeypatch.setattr(sys, "path", [search])
    fake_runtime.write_text(
        "import json, os\nfrom pathlib import Path\n"
        "p = json.loads(os.environ['PYBEAM_BOOTSTRAP'])\n"
        "Path(p['ready']).touch()\n"
        "pending = Path(p['completed'] + '.tmp')\n"
        "pending.write_text('0')\n"
        "pending.replace(p['completed'])\n"
        "pending = Path(os.environ['PYBEAM_CLEANUP'] + '.tmp')\n"
        "pending.write_text('clean')\n"
        "pending.replace(os.environ['PYBEAM_CLEANUP'])\n"
        "print(json.dumps([p['argv'], p['python_paths']]))\n"
    )
    assert _launch(entry) == 0
    assert json.loads(capfd.readouterr().out) == [
        [str(entry), "a", "--flag"],
        [str(entry.parent), search],
    ]


def test_process__merges_asset_environment_without_losing_caller_environment(
    fake_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PYBEAM_TEST_VALUE", "caller-value")
    monkeypatch.setenv("ROOTDIR", "stale-runtime")
    fake_runtime.write_text(
        "import json, os\nfrom pathlib import Path\n"
        "p = json.loads(os.environ['PYBEAM_BOOTSTRAP'])\n"
        "Path(p['ready']).touch()\n"
        "pending = Path(p['completed'] + '.tmp')\n"
        "pending.write_text('0')\n"
        "pending.replace(p['completed'])\n"
        "pending = Path(os.environ['PYBEAM_CLEANUP'] + '.tmp')\n"
        "pending.write_text('clean')\n"
        "pending.replace(os.environ['PYBEAM_CLEANUP'])\n"
        "print(json.dumps({key: os.environ[key] for key in "
        "['ROOTDIR', 'BINDIR', 'EMU', 'PROGNAME', 'PYBEAM_TEST_VALUE']}))\n"
    )
    assets = RuntimeAssets(
        Path(sys.executable), (), root_dir=fake_runtime.parent, bindir=fake_runtime.parent / "bin"
    )
    assert process.launch(fake_runtime, [], Config(), assets) == 0
    assert json.loads(capfd.readouterr().out) == {
        "ROOTDIR": str(fake_runtime.parent),
        "BINDIR": str(fake_runtime.parent / "bin"),
        "EMU": "beam",
        "PROGNAME": "erl",
        "PYBEAM_TEST_VALUE": "caller-value",
    }


def test_process__reports_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="Cannot start"):
        process.launch(tmp_path / "app.py", [], Config(), RuntimeAssets(tmp_path / "absent", ()))


@pytest.mark.parametrize("status", [1, 7, 42])
def test_process__propagates_nonzero_exit(fake_runtime: Path, status: int) -> None:
    fake_runtime.write_text("raise SystemExit({})\n".format(status))
    assert _launch(fake_runtime) == status


def test_process__rejects_success_before_ready(fake_runtime: Path) -> None:
    fake_runtime.write_text("pass\n")
    with pytest.raises(BootstrapError, match="before registering"):
        _launch(fake_runtime)


@pytest.mark.parametrize("status", [0, 7])
def test_process__application_stop_stall_after_completion_is_bounded_and_owned_pid_reaped(
    fake_runtime: Path,
    status: int,
) -> None:
    pidfile = fake_runtime.with_suffix(".pid")
    fake_runtime.write_text(
        "import json, os, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "p = json.loads(os.environ['PYBEAM_BOOTSTRAP'])\n"
        "Path(p['ready']).touch()\n"
        + "Path({!r}).write_text(str(os.getpid()))\n".format(str(pidfile))
        + "pending = Path(p['completed'] + '.tmp')\n"
        + "pending.write_text({!r})\n".format(str(status))
        + "pending.replace(p['completed'])\n"
        + "time.sleep(30)\n"
    )
    started = time.monotonic()
    with pytest.raises(CleanupError, match="shutdown deadline exceeded") as failure:
        _launch(fake_runtime, Config(startup_timeout=1, shutdown_timeout=0.15))
    assert failure.value.entry_status == status
    assert failure.value.exit_status == (status or 1)
    assert time.monotonic() - started < 3
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_process__ready_and_zero_exit_do_not_imply_cleanup_acknowledgment(
    fake_runtime: Path,
) -> None:
    fake_runtime.write_text(
        "import json, os\nfrom pathlib import Path\n"
        "p = json.loads(os.environ['PYBEAM_BOOTSTRAP'])\n"
        "Path(p['ready']).touch()\n"
        "pending = Path(p['completed']).with_suffix('.tmp')\n"
        "pending.write_text('0')\n"
        "pending.replace(p['completed'])\n"
    )
    with pytest.raises(CleanupError, match="not acknowledged") as failure:
        _launch(fake_runtime)
    assert failure.value.exit_status == 1


def test_process__startup_deadline_reaps_child(fake_runtime: Path) -> None:
    pidfile = fake_runtime.with_suffix(".pid")
    fake_runtime.write_text(
        "import os, time\nfrom pathlib import Path\n"
        + "Path({!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n".format(str(pidfile))
    )
    with pytest.raises(BootstrapError, match="startup deadline"):
        _launch(fake_runtime, Config(startup_timeout=0.3, shutdown_timeout=0.3))
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_process__kills_uncooperative_owned_group(fake_runtime: Path) -> None:
    pidfile = fake_runtime.with_suffix(".pid")
    fake_runtime.write_text(
        "import os, signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        + "Path({!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n".format(str(pidfile))
    )
    started = time.monotonic()
    with pytest.raises(BootstrapError, match="startup deadline"):
        _launch(fake_runtime, Config(startup_timeout=0.3, shutdown_timeout=0.1))
    assert time.monotonic() - started < 3
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_process__signal_during_spawn_is_not_lost(
    fake_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_runtime.write_text("import time\ntime.sleep(30)\n")

    def interrupted_command(
        _assets: RuntimeAssets,
        *,
        python_paths: tuple[str, ...] = (),
        startup: tuple[str, ...] = (),
    ) -> list[str]:
        os.kill(os.getpid(), signal.SIGTERM)
        return [sys.executable, str(fake_runtime)]

    monkeypatch.setattr(process, "_command", interrupted_command)
    with pytest.raises(CleanupError) as failure:
        _launch(fake_runtime, Config(shutdown_timeout=0.2))
    assert failure.value.exit_status == 143


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_process__forwards_signals_and_restores_handlers(fake_runtime: Path, number: int) -> None:
    fake_runtime.write_text(
        "import json, os, signal, time\nfrom pathlib import Path\n"
        "p = json.loads(os.environ['PYBEAM_BOOTSTRAP'])\n"
        "Path(p['ready']).touch()\n" + "os.kill(os.getppid(), {})\ntime.sleep(30)\n".format(number)
    )
    previous = signal.getsignal(number)
    with pytest.raises(CleanupError) as failure:
        _launch(fake_runtime, Config(shutdown_timeout=0.2))
    assert failure.value.exit_status == 128 + number
    assert signal.getsignal(number) == previous


@pytest.mark.parametrize("exit_code", [None, 0, 9])
async def test_loader__registers_and_invokes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int | None,
) -> None:
    monkeypatch.setattr(loader, "_state", loader._Loader())
    entry = tmp_path / "entry.py"
    calls = tmp_path / "calls.txt"
    ready = tmp_path / "ready"
    entry.write_text(
        "from pathlib import Path\nfrom pybeam import run\n"
        "async def main() -> None:\n"
        + "    Path({!r}).write_text('once')\n".format(str(calls))
        + ("" if exit_code is None else "    raise SystemExit({})\n".format(exit_code))
        + "if __name__ == '__main__': run(main)\n"
    )
    monkeypatch.setenv(
        "PYBEAM_BOOTSTRAP",
        json.dumps(
            {
                "argv": [str(entry), "argument"],
                "ready": str(ready),
                "completed": str(tmp_path / "completed"),
            }
        ),
    )
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    loader.load()
    assert not calls.exists()
    assert sys.argv == [str(entry), "argument"]
    assert await loader.invoke() == (exit_code or 0)
    assert ready.is_file()
    assert (tmp_path / "completed").read_text() == str(exit_code or 0)
    assert calls.read_text() == "once"
    calls.unlink()
    assert await loader.invoke() == (exit_code or 0)
    assert not calls.exists()


async def test_loader__completion_does_not_consume_runtime_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        pass

    completed = tmp_path / "completed"
    session = loader._Session(ready=tmp_path / "ready", completed=completed, entry=main)
    monkeypatch.setattr(loader, "_state", loader._Loader(session=session))
    runtime_pending = completed.with_name(completed.name + ".tmp")
    runtime_pending.write_text("143")

    assert await loader.invoke() == 0
    assert completed.read_text() == "0"
    runtime_pending.replace(completed)
    assert completed.read_text() == "143"


@pytest.mark.skipif(
    not sysconfig.get_config_var("Py_GIL_DISABLED"),
    reason="Only free-threaded builds immortalize code constants",
)
def test_loader__entry_constants_are_immortal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loader, "_state", loader._Loader())
    entry = tmp_path / "entry.py"
    entry.write_text(
        "from pybeam import run\nasync def main() -> None:\n    mask = 0xFFFFFFFF\nrun(main)\n"
    )
    monkeypatch.setenv(
        "PYBEAM_BOOTSTRAP",
        json.dumps(
            {
                "argv": [str(entry)],
                "ready": str(tmp_path / "ready"),
                "completed": str(tmp_path / "completed"),
            }
        ),
    )
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    loader.load()
    session = loader._state.session
    assert session is not None and isinstance(session.entry, FunctionType)
    mask = next(value for value in session.entry.__code__.co_consts if value == 0xFFFFFFFF)
    assert sys._is_immortal(mask)


@pytest.mark.parametrize("kind", ["py", "pyc", "pyz"])
async def test_loader__runs_source_bytecode_and_zip_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr(loader, "_state", loader._Loader())
    marker = tmp_path / "marker.txt"
    source = (
        "from pathlib import Path\nfrom pybeam import run\n"
        "async def main() -> None:\n"
        + "    Path({!r}).write_text(repr((__cached__, __package__)))\n".format(str(marker))
        + "run(main)\n"
    )
    script = tmp_path / "app.py"
    script.write_text(source)
    match kind:
        case "py":
            entry = script
        case "pyc":
            entry = tmp_path / "app.pyc"
            py_compile.compile(str(script), cfile=str(entry), doraise=True)
        case _:
            entry = tmp_path / "app.pyz"
            with zipfile.ZipFile(entry, "w") as archive:
                archive.writestr("__main__.py", source)
    monkeypatch.setenv(
        "PYBEAM_BOOTSTRAP",
        json.dumps(
            {
                "argv": [str(entry)],
                "ready": str(tmp_path / "ready"),
                "completed": str(tmp_path / "completed"),
            }
        ),
    )
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    loader.load()
    assert await loader.invoke() == 0
    if kind == "pyz":
        assert marker.is_file()
    else:
        assert marker.read_text() == repr((None, ""))


async def test_loader__reports_missing_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(loader, "_state", loader._Loader())
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    entry = tmp_path / "empty.py"
    entry.write_text("pass\n")
    monkeypatch.setenv(
        "PYBEAM_BOOTSTRAP",
        json.dumps(
            {
                "argv": [str(entry)],
                "ready": str(tmp_path / "ready"),
                "completed": str(tmp_path / "completed"),
            }
        ),
    )
    loader.load()
    assert await loader.invoke() == 1
    assert (tmp_path / "completed").read_text() == "1"
    assert "did not register" in capsys.readouterr().err


@pytest.mark.parametrize(
    "statement, status",
    [
        ("raise SystemExit(0)", 0),
        ("raise SystemExit(7)", 7),
        ("raise RuntimeError('load failed')", 1),
    ],
)
async def test_loader__early_file_exit_still_records_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
    status: int,
) -> None:
    monkeypatch.setattr(loader, "_state", loader._Loader())
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    entry = tmp_path / "early.py"
    entry.write_text(statement + "\n")
    completed = tmp_path / "completed"
    monkeypatch.setenv(
        "PYBEAM_BOOTSTRAP",
        json.dumps(
            {
                "argv": [str(entry)],
                "ready": str(tmp_path / "ready"),
                "completed": str(completed),
            }
        ),
    )
    loader.load()
    assert await loader.invoke() == status
    assert completed.read_text() == str(status)


async def test_shutdown__drains_successors_without_recancelling_finalizers() -> None:
    started = asyncio.Event()
    cleaned: list[str] = []
    tasks: list[asyncio.Task[None]] = []

    async def successor() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.02)
            cleaned.append("successor")

    async def background() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            tasks.append(asyncio.create_task(successor()))
            await started.wait()
            await asyncio.sleep(0.04)
            cleaned.append("original")

    tasks.append(asyncio.create_task(background()))
    await asyncio.sleep(0)
    await loader.shutdown(500)
    assert sorted(cleaned) == ["original", "successor"]
    assert all(task.done() and task.cancelling() == 1 for task in tasks)


async def test_shutdown__lets_taskgroup_owners_cancel_their_children_once() -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()
    children: list[asyncio.Task[None]] = []

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.04)
            cleaned.set()

    async def owner() -> None:
        async with asyncio.TaskGroup() as group:
            children.append(group.create_task(worker()))
            await asyncio.Event().wait()

    parent = asyncio.create_task(owner())
    await started.wait()
    await loader.shutdown(500)
    assert parent.done()
    assert cleaned.is_set()
    assert all(child.cancelling() == 1 for child in children)


async def test_shutdown__entrypoint_closes_detached_framework_lifespan_before_sweep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started, stop = asyncio.Event(), asyncio.Event()
    cleaned: list[str] = []
    framework_tasks: list[asyncio.Task[None]] = []

    async def lifespan() -> None:
        started.set()
        await stop.wait()
        cleaned.append("lifespan")

    async def entry() -> None:
        framework = asyncio.create_task(lifespan())
        framework_tasks.append(framework)
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            stop.set()
            await framework
            cleaned.append("entry")

    task = asyncio.create_task(entry())
    session = loader._Session(ready=tmp_path / "ready", completed=tmp_path / "completed")
    session.task = task
    monkeypatch.setattr(loader, "_state", loader._Loader(session=session))
    await started.wait()
    await loader.shutdown(500)
    assert cleaned == ["lifespan", "entry"]
    assert task.cancelled()
    assert all(task.done() and not task.cancelled() for task in framework_tasks)


def _native_runtime(request: pytest.FixtureRequest) -> Path:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and a Python 3.14 native runtime")
    runtime_option: object = request.config.getoption("hornbeam_runtime")
    assert isinstance(runtime_option, str)
    runtime = Path(runtime_option).resolve()
    assert development_assets(runtime).code_paths
    return runtime


@pytest.mark.runtime
@pytest.mark.parametrize("cli", [False, True])
def test_native__file_and_cli_enter_main_once(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    cli: bool,
) -> None:
    runtime = _native_runtime(request)
    record = tmp_path / "calls.txt"
    entry = tmp_path / "entry.py"
    entry.write_text(
        "import asyncio, json, os, sys\nfrom pathlib import Path\nfrom pybeam import run\n"
        + "record = Path({!r})\n".format(str(record))
        + "with record.open('a') as out: out.write('load\\n')\n"
        "async def main() -> None:\n"
        "    await asyncio.sleep(0.01)\n"
        "    with record.open('a') as out: out.write('main\\n')\n"
        "    print(json.dumps([sys.argv[1:], os.getcwd(), os.environ['PYBEAM_TEST_VALUE']]), flush=True)\n"
        "    print('stdin=' + sys.stdin.readline().strip(), flush=True)\n"
        "    print('entry stderr', file=sys.stderr, flush=True)\n"
        "if __name__ == '__main__': run(main)\n"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(runtime) + '"\n'
    )
    command = ["uv", "run", "--project", str(ROOT)]
    command.extend(["pybeam", "run"] if cli else ["python"])
    environment = dict(os.environ)
    environment["PYBEAM_TEST_VALUE"] = "inherited"
    result = subprocess.run(
        [*command, str(entry), "one", "--two"],
        input="managed input\n",
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert record.read_text().splitlines() == (
        ["load", "main"] if cli else ["load", "load", "main"]
    )
    assert json.dumps([["one", "--two"], str(tmp_path), "inherited"]) in result.stdout
    assert "stdin=managed input" in result.stdout
    assert "entry stderr" in result.stderr


@pytest.mark.runtime
@pytest.mark.parametrize(
    "statement, status",
    [
        ("raise SystemExit(7)", 7),
        ("raise RuntimeError('entry exploded')", 1),
    ],
)
def test_native__reports_entry_failure(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    statement: str,
    status: int,
) -> None:
    runtime = _native_runtime(request)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(runtime) + '"\n'
    )
    entry = tmp_path / "failure.py"
    entry.write_text(
        "from pybeam import run\nasync def main() -> None:\n    "
        + statement
        + "\nif __name__ == '__main__': run(main)\n"
    )
    result = subprocess.run(
        [sys.executable, str(entry)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == status, result.stdout + result.stderr
    if status == 1:
        assert "entry exploded" in result.stderr


@pytest.mark.runtime
@pytest.mark.parametrize(
    "statement, status",
    [
        ("raise SystemExit(0)", 0),
        ("raise SystemExit(7)", 7),
        ("raise RuntimeError('early failure')", 1),
    ],
)
def test_native__early_file_exit_cleans_vm(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    statement: str,
    status: int,
) -> None:
    runtime = _native_runtime(request)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(runtime) + '"\n'
    )
    pidfile = tmp_path / "vm.pid"
    entry = tmp_path / "early.py"
    entry.write_text(
        "import os\nfrom pathlib import Path\n"
        + "Path({!r}).write_text(str(os.getpid()))\n".format(str(pidfile))
        + statement
        + "\n"
    )
    result = subprocess.run(
        ["uv", "run", "--project", str(ROOT), "pybeam", "run", str(entry)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == status, result.stdout + result.stderr
    assert "cleanup failed" not in result.stderr
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.runtime
def test_native__application_stop_stall_has_parent_deadline(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = development_assets(_native_runtime(request))
    helper = tmp_path / "pybeam_stop_stall.erl"
    helper.write_text(
        "-module(pybeam_stop_stall).\n-export([start/2, stop/1]).\n"
        "start(_, _) -> {ok, spawn_link(fun() -> receive stop -> ok end end)}.\n"
        "stop(_) ->\n"
        + '    ok = file:write_file("'
        + str(tmp_path / "stop_entered")
        + '", <<"entered">>),\n'
        + "    receive never -> ok end.\n"
    )
    subprocess.run(["erlc", "-Werror", "-o", str(tmp_path), str(helper)], check=True)
    pidfile = tmp_path / "helper.pid"
    expression = (
        "ok = application:load({application, pybeam_stop_stall, "
        '[{description, "stop stall"}, {vsn, "1"}, {modules, [pybeam_stop_stall]}, '
        "{applications, [kernel, stdlib]}, {mod, {pybeam_stop_stall, []}}]}), "
        "ok = application:start(pybeam_stop_stall), "
        + 'ok = file:write_file("'
        + str(pidfile)
        + '", os:getpid()), '
        + 'Completion = os:getenv("PYBEAM_COMPLETION"), '
        + 'ok = file:write_file(Completion ++ ".tmp", <<"0">>), '
        + 'ok = file:rename(Completion ++ ".tmp", Completion), '
        + "application:stop(pybeam_stop_stall), halt(0)."
    )

    def command(
        _assets: RuntimeAssets,
        *,
        python_paths: tuple[str, ...] = (),
        startup: tuple[str, ...] = (),
    ) -> list[str]:
        return [
            str(assets.executable),
            "+S",
            "1:1",
            "+Bi",
            "-noshell",
            "-noinput",
            "-pa",
            str(tmp_path),
            "-eval",
            expression,
        ]

    monkeypatch.setattr(process, "_command", command)
    with pytest.raises(CleanupError, match="shutdown deadline exceeded"):
        process.launch(helper, [], Config(shutdown_timeout=0.2), assets)
    assert (tmp_path / "stop_entered").is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.runtime
@pytest.mark.parametrize("status", [0, 7])
@pytest.mark.parametrize("shutdown_timeout", [0.15, 2.0])
def test_native__cancellation_resistant_background_task_cannot_report_clean_success(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    status: int,
    shutdown_timeout: float,
) -> None:
    runtime = _native_runtime(request)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "'
        + str(runtime)
        + '"\nshutdown_timeout = {}\n'.format(shutdown_timeout)
    )
    pidfile = tmp_path / "vm.pid"
    cancelled = tmp_path / "cancelled"
    entry = tmp_path / "resistant.py"
    entry.write_text(
        "import asyncio, os\nfrom pathlib import Path\nfrom pybeam import run\n"
        "tasks: list[asyncio.Task[None]] = []\n"
        "async def stubborn() -> None:\n"
        "    while True:\n"
        "        try:\n            await asyncio.sleep(0.01)\n"
        "        except asyncio.CancelledError:\n"
        + "            Path({!r}).touch()\n".format(str(cancelled))
        + "async def main() -> None:\n"
        "    tasks.append(asyncio.create_task(stubborn()))\n"
        "    await asyncio.sleep(0.02)\n"
        + "    Path({!r}).write_text(str(os.getpid()))\n".format(str(pidfile))
        + ("" if status == 0 else "    raise SystemExit({})\n".format(status))
        + "if __name__ == '__main__': run(main)\n"
    )
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(entry)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == (status or 1), result.stdout + result.stderr
    assert "cleanup" in result.stderr, result.stdout + result.stderr
    assert time.monotonic() - started < 8
    assert cancelled.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.runtime
def test_native__cooperative_background_cleanup_uses_configured_five_second_budget(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    runtime = _native_runtime(request)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(runtime) + '"\nshutdown_timeout = 5\n'
    )
    cleaned = tmp_path / "cleaned"
    pidfile = tmp_path / "vm.pid"
    entry = tmp_path / "cooperative.py"
    entry.write_text(
        "import asyncio, os\nfrom pathlib import Path\nfrom pybeam import run\n"
        "tasks: list[asyncio.Task[None]] = []\nstarted = asyncio.Event()\n"
        "async def worker() -> None:\n"
        "    started.set()\n"
        "    try:\n        await asyncio.Event().wait()\n"
        "    finally:\n        await asyncio.sleep(1.3)\n"
        + "        Path({!r}).touch()\n".format(str(cleaned))
        + "async def grouped() -> None:\n"
        "    async with asyncio.TaskGroup() as group:\n"
        "        group.create_task(worker())\n"
        "        await asyncio.Event().wait()\n"
        "async def main() -> None:\n"
        + "    Path({!r}).write_text(str(os.getpid()))\n".format(str(pidfile))
        + "    tasks.append(asyncio.create_task(grouped()))\n"
        "    await started.wait()\n"
        "if __name__ == '__main__': run(main)\n"
    )
    result = subprocess.run(
        [sys.executable, str(entry)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert cleaned.is_file()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.runtime
@pytest.mark.parametrize("successors", [1, 3, -1])
def test_native__requires_task_quiescence_including_cancellation_successors(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    successors: int,
) -> None:
    runtime = _native_runtime(request)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(runtime) + '"\nshutdown_timeout = 0.4\n'
    )
    record = tmp_path / "events"
    pidfile = tmp_path / "vm.pid"
    entry = tmp_path / "successors.py"
    entry.write_text(
        "import asyncio, os\nfrom pathlib import Path\nfrom pybeam import run\n"
        + "record = Path({!r})\n".format(str(record))
        + "tasks: list[asyncio.Task[None]] = []\n"
        "def event(text: str) -> None:\n"
        "    with record.open('a') as out: out.write(text + '\\n')\n"
        "async def background(left: int, started: asyncio.Event) -> None:\n"
        "    started.set()\n    event('started')\n"
        "    try:\n        await asyncio.Event().wait()\n"
        "    finally:\n"
        "        if left != 0:\n"
        "            child_started = asyncio.Event()\n"
        "            tasks.append(asyncio.create_task(background(left - 1, child_started)))\n"
        "            await child_started.wait()\n"
        "        await asyncio.sleep(0.01)\n"
        "        event('cleaned')\n"
        "async def main() -> None:\n"
        + "    Path({!r}).write_text(str(os.getpid()))\n".format(str(pidfile))
        + "    started = asyncio.Event()\n"
        + "    tasks.append(asyncio.create_task(background({}, started)))\n".format(successors)
        + "    await started.wait()\n"
        "if __name__ == '__main__': run(main)\n"
    )
    result = subprocess.run(
        [sys.executable, str(entry)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    events = record.read_text().splitlines()
    if successors >= 0:
        assert result.returncode == 0, result.stdout + result.stderr
        assert events.count("started") == successors + 1
        assert events.count("cleaned") == successors + 1
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "cleanup" in result.stderr
        assert events.count("started") > 1
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.runtime
@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_native__signals_cancel_main_and_reap_vm(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    number: int,
) -> None:
    runtime = _native_runtime(request)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pybeam]\ndevelopment_runtime = "' + str(runtime) + '"\nshutdown_timeout = 5\n'
    )
    entry = tmp_path / "signal_entry.py"
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"
    entry.write_text(
        "import asyncio, os\nfrom pathlib import Path\nfrom pybeam import run\n"
        "async def main() -> None:\n"
        + "    Path({!r}).write_text(str(os.getpid()))\n".format(str(started))
        + "    try:\n        await asyncio.Event().wait()\n    finally:\n"
        + "        task = asyncio.current_task()\n"
        + "        assert task is not None and task.cancelling() == 1\n"
        + "        await asyncio.sleep(1.3)\n"
        + "        assert task.cancelling() == 1\n"
        + "        Path({!r}).touch()\n".format(str(stopped))
        + "if __name__ == '__main__': run(main)\n"
    )
    child = subprocess.Popen(
        [sys.executable, str(entry)],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not started.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists(), child.communicate(timeout=3)
        vm_pid = int(started.read_text())
        child.send_signal(number)
        stdout, stderr = child.communicate(timeout=8)
        assert child.returncode == 128 + number, stdout + stderr
        assert stopped.exists(), stdout + stderr
        with pytest.raises(ProcessLookupError):
            os.kill(vm_pid, 0)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
