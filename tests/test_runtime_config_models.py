from pathlib import Path

import pytest
from pydantic import ValidationError

from pybeam.runtime.config import BootstrapError, Config, Mode, load_config, parse_config


@pytest.mark.parametrize("field", ["startup_timeout", "shutdown_timeout"])
@pytest.mark.parametrize(
    "value,error_type",
    [
        (0, "greater_than"),
        (-1.0, "greater_than"),
        (True, "float_type"),
        ("1", "float_type"),
        (float("inf"), "finite_number"),
        (float("-inf"), "finite_number"),
        (float("nan"), "finite_number"),
    ],
)
def test_config__rejects_invalid_timeout_at_boundary(
    field: str, value: object, error_type: str, tmp_path: Path
) -> None:
    with pytest.raises(BootstrapError) as caught:
        parse_config({field: value}, tmp_path)
    error = caught.value.__cause__
    assert isinstance(error, ValidationError)
    assert error.errors()[0]["loc"] == (field,)
    assert error.errors()[0]["type"] == error_type


@pytest.mark.parametrize(
    "value,location,error_type",
    [
        ({"workers": True}, ("workers",), "int_type"),
        ({"workers": "1"}, ("workers",), "int_type"),
        ({"workers": 1.0}, ("workers",), "int_type"),
        ({"workers": 0}, ("workers",), "greater_than_equal"),
        ({"workers": 2}, ("workers",), "less_than_equal"),
        ({"distribution": 0}, ("distribution",), "bool_type"),
        ({"listeners": "false"}, ("listeners",), "bool_type"),
        ({"development_runtime": ""}, ("development_runtime",), "string_too_short"),
        ({"development_runtime": 1}, ("development_runtime",), "string_type"),
        ({"unknown": 1}, ("unknown",), "extra_forbidden"),
        ([], (), "model_type"),
        ({"mode": 1}, ("mode",), "string_type"),
    ],
)
def test_config__exposes_structured_boundary_errors(
    value: object, location: tuple[str, ...], error_type: str, tmp_path: Path
) -> None:
    with pytest.raises(BootstrapError) as caught:
        parse_config(value, tmp_path)
    error = caught.value.__cause__
    assert isinstance(error, ValidationError)
    assert error.errors()[0]["loc"] == location
    assert error.errors()[0]["type"] == error_type


@pytest.mark.parametrize("mode", ["external", "custom"])
def test_config__fails_clearly_for_unsupported_modes(mode: str, tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="not implemented"):
        parse_config({"mode": mode}, tmp_path)


@pytest.mark.parametrize("field", ["distribution", "listeners"])
def test_config__fails_closed_for_unsupported_capabilities(field: str, tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="opens no listeners"):
        parse_config({field: True}, tmp_path)


def test_config__resolves_path_relative_to_project_once(tmp_path: Path) -> None:
    config = parse_config({"development_runtime": "nested/../runtime"}, tmp_path)
    assert config.development_runtime == tmp_path / "runtime"
    assert Config.model_validate(config, context=tmp_path / "other") is config
    assert config.mode is Mode.MANAGED
    assert config.workers == 1
    assert config.distribution is False
    assert config.listeners is False


def test_config__accepts_integer_seconds_without_accepting_booleans(tmp_path: Path) -> None:
    config = parse_config({"startup_timeout": 2, "shutdown_timeout": 1}, tmp_path)
    assert config.startup_timeout == 2.0
    assert config.shutdown_timeout == 1.0


def test_config__public_construction_enforces_constraints_and_freezing() -> None:
    with pytest.raises(ValidationError) as caught:
        Config(startup_timeout=0)
    assert caught.value.errors()[0]["type"] == "greater_than"
    config = Config()
    with pytest.raises(ValidationError) as frozen:
        config.__setattr__("startup_timeout", 1.0)
    assert frozen.value.errors()[0]["type"] == "frozen_instance"


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
        ('tool = "invalid"\n', ("tool",)),
        ('[tool]\npybeam = "invalid"\n', ("tool", "pybeam")),
        ("[tool.pybeam]\nunknown = true\n", ("tool", "pybeam", "unknown")),
    ],
)
def test_load_config__validates_tables_and_rejects_unknown_settings(
    document: str, location: tuple[str, ...], tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(document)
    with pytest.raises(BootstrapError) as caught:
        load_config(tmp_path)
    error = caught.value.__cause__
    assert isinstance(error, ValidationError)
    assert error.errors()[0]["loc"] == location
