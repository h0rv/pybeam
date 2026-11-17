from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from pybeam.codec import CodecError
from pybeam.runtime.config import BootstrapError, Config, Mode, load_config, parse_config


@pytest.mark.parametrize("field", ["startup_timeout", "shutdown_timeout"])
@pytest.mark.parametrize(
    "value,error_type",
    [
        (0, "positive finite"),
        (-1.0, "positive finite"),
        (True, "expected float"),
        ("1", "expected float"),
        (float("inf"), "finite"),
        (float("-inf"), "finite"),
        (float("nan"), "finite"),
    ],
)
def test_config__rejects_invalid_timeout_at_boundary(
    field: str, value: object, error_type: str, tmp_path: Path
) -> None:
    with pytest.raises(BootstrapError) as caught:
        parse_config({field: value}, tmp_path)
    error = caught.value.__cause__
    assert isinstance(error, CodecError)
    assert field in str(error)
    assert error_type in str(error)


@pytest.mark.parametrize(
    "value,location,error_type",
    [
        ({"workers": True}, "$.workers", "expected one of"),
        ({"workers": "1"}, "$.workers", "expected one of"),
        ({"workers": 1.0}, "$.workers", "expected one of"),
        ({"workers": 0}, "$.workers", "expected one of"),
        ({"workers": 2}, "$.workers", "expected one of"),
        ({"listeners": "false"}, "$.listeners", "expected bool"),
        ({"development_runtime": 1}, "$.development_runtime", "expected"),
        ({"unknown": 1}, "$", "unexpected fields: unknown"),
        ([], "$", "expected object"),
        ({"mode": 1}, "$.mode", "expected"),
    ],
)
def test_config__exposes_structured_boundary_errors(
    value: object, location: str, error_type: str, tmp_path: Path
) -> None:
    with pytest.raises(BootstrapError) as caught:
        parse_config(value, tmp_path)
    error = caught.value.__cause__
    assert isinstance(error, CodecError)
    assert str(error).startswith(location + ":")
    assert error_type in str(error)


@pytest.mark.parametrize("value", [{"distribution": 0}, {"development_runtime": ""}])
def test_config__rejects_malformed_values_before_decoding(value: object, tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match=r"Invalid tool\.pybeam configuration"):
        parse_config(value, tmp_path)


@pytest.mark.parametrize("mode", ["external", "custom"])
def test_config__fails_clearly_for_unsupported_modes(mode: str, tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="not implemented"):
        parse_config({"mode": mode}, tmp_path)


@pytest.mark.parametrize(
    "field,message",
    [("distribution", "explicit two-node configuration"), ("listeners", "opens no listeners")],
)
def test_config__fails_closed_for_unsupported_capabilities(
    field: str, message: str, tmp_path: Path
) -> None:
    with pytest.raises(BootstrapError, match=message):
        parse_config({field: True}, tmp_path)


def test_config__resolves_path_relative_to_project_once(tmp_path: Path) -> None:
    config = parse_config({"development_runtime": "nested/../runtime"}, tmp_path)
    assert config.development_runtime == tmp_path / "runtime"
    assert config.mode is Mode.MANAGED
    assert config.workers == 1
    assert config.distribution is False
    assert config.listeners is False


def test_config__accepts_integer_seconds_without_accepting_booleans(tmp_path: Path) -> None:
    config = parse_config({"startup_timeout": 2, "shutdown_timeout": 1}, tmp_path)
    assert config.startup_timeout == 2.0
    assert config.shutdown_timeout == 1.0


def test_config__public_construction_enforces_constraints_and_freezing() -> None:
    with pytest.raises(ValueError, match="startup_timeout"):
        Config(startup_timeout=0)
    config = Config()
    with pytest.raises(FrozenInstanceError):
        config.__setattr__("startup_timeout", 1.0)


def test_load_config__parses_native_toml_values_and_ignores_other_tools(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\n'
        "[tool.other]\noption = true\n"
        '[tool.pybeam]\ndevelopment_runtime = "runtime"\nstartup_timeout = 2\n'
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    config = load_config(nested)
    assert config.development_runtime == tmp_path / "runtime"
    assert config.startup_timeout == 2.0


@pytest.mark.parametrize(
    "document,location",
    [
        ('tool = "invalid"\n', "tool must be a table"),
        ('[tool]\npybeam = "invalid"\n', "expected object"),
        ("[tool.pybeam]\nunknown = true\n", "unexpected fields: unknown"),
    ],
)
def test_load_config__validates_tables_and_rejects_unknown_settings(
    document: str, location: str, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(document)
    with pytest.raises(BootstrapError, match=location):
        load_config(tmp_path)
