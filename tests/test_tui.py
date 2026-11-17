import base64
import os
import subprocess
import sys
import time

import pytest

from pybeam.codec import CodecError
from pybeam.runtime.attach import Connection
from pybeam.runtime.config import BootstrapError
from pybeam.runtime.inspector import REPORT, Report, Resource, Stat
from pybeam.runtime.tui import Selection, _clean, _summary


def test_report__parses_typed_metrics_and_rejects_invalid_data() -> None:
    resource = Resource("actor", "group/0", "Counter-0", "running", (Stat("Free slots", "4"),))
    report = Report((resource,), ("Queues",))
    assert REPORT.decode(REPORT.encode(report)) == report
    with pytest.raises(CodecError):
        REPORT.decode('{"resources": [{"kind": "secret"}], "unavailable": []}')
    with pytest.raises(CodecError):
        REPORT.decode("{bad json")
    assert _summary(resource) == "Free slots 4"


def test_selection__clamps_empty_lists_and_supports_navigation() -> None:
    selection = Selection()
    selection.move(ord("j"), 0)
    assert selection.index == 0
    selection.move(ord("j"), 2)
    selection.move(ord("j"), 2)
    assert selection.index == 1
    selection.move(ord("k"), 2)
    assert selection.index == 0
    assert _clean("queue\x1b[31m\nname\t") == "queue [31m name "


def test_connection__timeout_is_bounded_and_stops_client() -> None:
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.readline(); sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    ) as process:
        started = time.monotonic()
        with pytest.raises(BootstrapError, match="timed out"):
            Connection(process).request("inspect", "", timeout=0.05)
        assert time.monotonic() - started < 3
        process.wait(timeout=3)


def test_connection__collects_partial_reply_without_waiting_for_eof() -> None:
    reply = base64.b64encode(b"hello").decode()
    script = (
        "import sys, time; sys.stdin.readline(); "
        "sys.stdout.write(" + repr(reply[:2]) + "); sys.stdout.flush(); time.sleep(0.02); "
        "sys.stdout.write(" + repr(reply[2:] + "\n") + "); sys.stdout.flush(); sys.stdin.read()"
    )
    with subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as process:
        assert Connection(process).request("inspect", "", timeout=3) == "hello"
        assert process.stdin is not None
        process.stdin.close()


def test_connection__closed_client_reports_detach() -> None:
    with subprocess.Popen(
        [sys.executable, "-c", "pass"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as process:
        process.wait(timeout=3)
        with pytest.raises(BootstrapError, match="closed"):
            Connection(process).request("inspect", "", timeout=0.1)


def test_cli_tui__rejects_pipes_with_actionable_message() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from pybeam.runtime.cli import main; main()", "tui"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        env=dict(os.environ),
        check=False,
    )
    assert result.returncode == 1
    assert "needs a terminal" in result.stderr
    assert "pybeam ps" in result.stderr
