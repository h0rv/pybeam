import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, assert_type

import pytest

from tests.native.runtime import NATIVE, ROOT, host_python_paths, require_runtime

if TYPE_CHECKING:
    from erlang.actors import OwnedActor

    async def _checked_call(actor: OwnedActor) -> None:
        async def method(_state: object, value: int, *, label: str) -> str:
            return label * value

        assert_type(await actor.call(0, method, 2, label="ok"), str)


def test_public_runtime_api__ordinary_interpreter_requires_native_loop(tmp_path: Path) -> None:
    code = """
import asyncio
import importlib.util
import sys
sys.path.insert(0, PRIV)
assert importlib.util.find_spec('erlang') is None
from erlang_services import ServiceSlot
from pybeam.actors.native import spawn
from pybeam.supervision.ref import SupervisorRef
async def service():
    pass
async def verify():
    try:
        ServiceSlot(capacity=1).start(service)
    except RuntimeError as error:
        assert str(error) == 'services require a running ErlangEventLoop'
        assert isinstance(error.__cause__, ModuleNotFoundError)
    else:
        raise AssertionError('ordinary asyncio must not admit native services')
asyncio.run(verify())
assert '_erlang_impl' not in sys.modules
""".replace("PRIV", repr(str(NATIVE / "priv")))
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


NATIVE_PROBE = """
import asyncio
import sys
import erlang
import erlang.actors
import erlang.supervision
from erlang.actors import OwnedActor
from erlang.supervision import NativeSupervisor
from erlang_services import ServiceSlot
from pybeam.actors.native import spawn
from pybeam.actors.protocol import ActorSpec
from pybeam.mailbox import UnboundedMailbox

class Counter:
    def __init__(self):
        self.value = 0
    async def add(self, amount, *, label):
        await asyncio.sleep(0)
        self.value += amount
        return (self.value, label)

async def service():
    await asyncio.sleep(0)

async def verify():
    assert sys.modules['erlang.actors'] is erlang.actors
    assert sys.modules['erlang.supervision'] is erlang.supervision
    assert OwnedActor is erlang.actors.OwnedActor
    assert NativeSupervisor is erlang.supervision.NativeSupervisor
    assert isinstance(asyncio.get_running_loop(), erlang.ErlangEventLoop)
    actor = await spawn(ActorSpec(Counter, (Counter.add,)), mailbox=UnboundedMailbox())
    try:
        assert await actor.call(Counter.add, 2, label='public') == (2, 'public')
        assert await actor.call(Counter.add, 3, label='again') == (5, 'again')
    finally:
        await actor.stop()
    ref = ServiceSlot(capacity=1).start(service)
    await ref.wait(timeout=2)
    return 'public-api-ok'
"""


@pytest.mark.runtime
def test_public_runtime_api__native_exports_and_execution(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    runtime = require_runtime(request)
    (tmp_path / "public_runtime_probe.py").write_text(NATIVE_PROBE)
    code = """
        application:load(erlang_python),
        application:set_env(erlang_python, num_contexts, 1),
        application:set_env(erlang_python, event_loop_pool_size, 1),
        {ok, _} = application:ensure_all_started(erlang_python),
        ok = py_import:add_paths(HOST_PATHS),
        {ok, W} = py_context:new(#{mode => worker}),
        ok = py_context:exec(W, <<PYTHON_PATHS>>),
        ok = py_context:exec(W, <<"import public_runtime_probe">>),
        ok = py_context:start_loop(W),
        {ok, _} = py_context:submit_await(W, public_runtime_probe, verify, [], #{}, 10000),
        ok = py_context:stop_loop(W, 1000),
        receive {py_loop_exit, W, _} -> ok after 5000 -> error(missing_loop_exit) end,
        ok = py_context:stop(W),
        ok = application:stop(erlang_python),
        io:format("public-runtime-api-ok~n"),
        halt(0).
    """.replace("HOST_PATHS", host_python_paths()).replace(
        "PYTHON_PATHS",
        json.dumps(f"import sys; sys.path[:0] = {[str(ROOT / 'src'), str(tmp_path)]!r}"),
    )
    environment = os.environ.copy()
    environment["ERL_CRASH_DUMP"] = str(tmp_path / "erl_crash.dump")
    result = subprocess.run(
        ["erl", "+S", "2:2", "-noshell", "-pa", str(runtime / "ebin"), "-eval", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    (tmp_path / "public-runtime-api.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "public-runtime-api-ok" in result.stdout
