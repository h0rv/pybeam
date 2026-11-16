import tomllib
from pathlib import Path

import pytest
from pydantic import BaseModel, TypeAdapter

from tools.genstage_assets import APPROVED_WHEEL, HELPER_SHA256, approved_artifact, stage
from tools.genstage_fixtures import FIXTURES, SOURCES

ROOT = Path(__file__).resolve().parents[1]


def test_reviewed_wheels_keep_separate_platform_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tools.genstage_assets.platform.system", lambda: "Darwin")
    monkeypatch.setattr("tools.genstage_assets.platform.machine", lambda: "arm64")
    darwin = approved_artifact()
    assert (darwin.wheel, darwin.helper) == (APPROVED_WHEEL, HELPER_SHA256)
    monkeypatch.setattr("tools.genstage_assets.platform.system", lambda: "Linux")
    monkeypatch.setattr("tools.genstage_assets.platform.machine", lambda: "x86_64")
    linux = approved_artifact()
    assert linux.wheel != darwin.wheel
    assert linux.helper != darwin.helper
    assert linux.nif != darwin.nif
    monkeypatch.setattr("tools.genstage_assets.platform.machine", lambda: "arm64")
    with pytest.raises(ValueError, match="Unsupported optional wheel platform"):
        approved_artifact()


def test_typing__retains_the_public_sibling_contract() -> None:
    paths = TypeAdapter(list[str]).validate_python(
        tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["ty"]["environment"][
            "extra-paths"
        ]
    )
    assert paths == [
        "typings",
        ".",
        "../erlang-python/priv",
    ]
    assert (ROOT / "typings/erlang-stubs/actors.pyi").is_file()


class Task(BaseModel):
    run: str | list[str]


def test_genstage_commands__do_not_require_private_checkout_inputs() -> None:
    tasks = TypeAdapter(dict[str, Task]).validate_python(
        tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]
    )
    assert '--out-dir "${usage_output}/wheel"' in tasks["build:optional-genstage"].run
    for name, task in tasks.items():
        if "genstage" in name:
            command = task.run
            assert isinstance(command, str)
            assert ".experiment" not in command
            assert "native-genstage" not in command
            assert "worktrees/" not in command
    for file in ("test_genstage_wheel.py", "test_packaging.py"):
        source = (ROOT / "tests" / file).read_text()
        assert ".experiment" not in source
        assert "native-genstage" not in source


def test_installed_fixture_sources__are_available_in_the_tracked_test_tree() -> None:
    assert SOURCES == ROOT / "tests/native"
    assert len(FIXTURES) == 6
    assert all((SOURCES / name).is_file() for name in FIXTURES)
    assert not any((SOURCES / name).is_symlink() for name in FIXTURES)


def test_asset_preparation__rejects_unapproved_input_without_creating_output(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "unapproved.whl"
    wheel.write_bytes(b"not the reviewed artifact")
    output = tmp_path / "assets"
    with pytest.raises(ValueError, match="exact reviewed"):
        stage(wheel, output)
    assert not output.exists()
