import os
from pathlib import Path
from typing import Never

import pytest

from pybeam.runtime import process
from pybeam.runtime.assets import RuntimeAssets
from pybeam.runtime.config import BootstrapError, Config


@pytest.mark.parametrize("inherited", ["absent", "0", "1"])
def test_managed_startup__disables_guest_bytecode_before_process_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inherited: str
) -> None:
    if inherited == "absent":
        monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    else:
        monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", inherited)
    before = dict(os.environ)

    def refused(command: list[str], *, env: dict[str, str], start_new_session: bool) -> Never:
        assert command[0] == "unused-erlexec"
        assert start_new_session
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert dict(os.environ) == before
        raise OSError("No VM is created in this startup-boundary unit control")

    monkeypatch.setattr(process.subprocess, "Popen", refused)
    with pytest.raises(BootstrapError, match="Cannot start the managed runtime"):
        process.launch(
            tmp_path / "entry.py",
            [],
            Config(),
            RuntimeAssets(executable=Path("unused-erlexec"), code_paths=(tmp_path,)),
            extra_environment={"PYTHONDONTWRITEBYTECODE": "0"},
        )
    assert dict(os.environ) == before
