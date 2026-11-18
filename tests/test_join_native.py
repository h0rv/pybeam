import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import ROOT, is_address

pytestmark = pytest.mark.runtime

_HOST = """
false = lists:keymember(erlang_python, 1, application:loaded_applications()),
non_existing = code:which(py),
true = register(join_receiver, self()),
ok = net_kernel:monitor_nodes(true),
Worker = 'pybeam@127.0.0.1',
case lists:member(Worker, nodes()) of
    true -> ok;
    false -> receive {nodeup, Worker} -> ok after 12000 -> error(no_join) end
end,
Ctx = erpc:call(Worker, py, context, [1]),
{ok, 9.0} = erpc:call(Worker, py_context, submit_await, [Ctx, math, sqrt, [81.0]]),
{ok, 42} = erpc:call(Worker, py_context, submit_await,
                    [Ctx, join_work, reply, [self(), atom_to_binary(node())]], 10000),
receive {<<"pid">>, 42} -> ok after 5000 -> error(no_pid_reply) end,
receive {<<"named">>, 42} -> ok after 5000 -> error(no_named_reply) end,
false = lists:keymember(erlang_python, 1, application:loaded_applications()),
non_existing = code:which(py),
io:format("JOIN_RPC_AND_REPLIES_OK~n"),
halt(0).
"""


def test_join__plain_host_calls_python_and_receives_pid_and_named_replies(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    if request.config.getoption("run_runtime") is not True:
        pytest.skip("Requires --run-runtime and a runtime with remote PID sends")
    profile: object = request.config.getoption("hornbeam_runtime")
    assert isinstance(profile, str)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pybeam]\ndevelopment_runtime = " + json.dumps(str(Path(profile).resolve())) + "\n"
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        address: object = listener.getsockname()
        assert is_address(address)
        port = str(address[1])
    environment = dict(
        os.environ,
        PYTHONPATH=str(ROOT / "src"),
        PYBEAM_CLUSTER="host@127.0.0.1",
        PYBEAM_COOKIE=secrets.token_hex(32),
        ERL_EPMD_ADDRESS="127.0.0.1",
        ERL_EPMD_PORT=port,
        ERL_FLAGS="+S 2:2 +SDcpu 2 +SDio 2",
        JOIN_READY=str(tmp_path / "worker.ready"),
    )
    with (
        (tmp_path / "epmd.log").open("wb") as epmd_log,
        (tmp_path / "worker.log").open("wb") as worker_log,
    ):
        epmd = subprocess.Popen(
            ["epmd", "-port", port],
            env=environment,
            stdout=epmd_log,
            stderr=epmd_log,
        )
        try:
            deadline = time.monotonic() + 5
            while True:
                assert epmd.poll() is None, (tmp_path / "epmd.log").read_text()
                try:
                    with socket.create_connection(("127.0.0.1", int(port)), timeout=0.1):
                        break
                except OSError:
                    assert time.monotonic() < deadline, "EPMD did not start"
                    time.sleep(0.02)
            worker = subprocess.Popen(
                [sys.executable, str(ROOT / "tests/native/join_entry.py")],
                cwd=tmp_path,
                env=environment,
                stdout=worker_log,
                stderr=worker_log,
            )
            try:
                deadline = time.monotonic() + 30
                while not (tmp_path / "worker.ready").exists():
                    assert worker.poll() is None, (tmp_path / "worker.log").read_text()
                    assert time.monotonic() < deadline, (tmp_path / "worker.log").read_text()
                    time.sleep(0.02)
                host = subprocess.run(
                    [
                        "erl",
                        "-noshell",
                        "-name",
                        "host@127.0.0.1",
                        "-setcookie",
                        environment["PYBEAM_COOKIE"],
                        "-kernel",
                        "inet_dist_use_interface",
                        "{127,0,0,1}",
                        "-eval",
                        _HOST,
                    ],
                    cwd=tmp_path,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                assert host.returncode == 0, (
                    host.stdout + host.stderr + (tmp_path / "worker.log").read_text()
                )
                assert "JOIN_RPC_AND_REPLIES_OK" in host.stdout
            finally:
                worker.terminate()
                worker.wait(timeout=20)
        finally:
            epmd.terminate()
            epmd.wait(timeout=5)
