from pathlib import Path

from fastapi.testclient import TestClient

from benchmarks.profiles import ROOT, configuration
from examples.showcase.app import Settings, create_app
from examples.showcase.catalog import catalog


def test_catalog__three_fixed_profiles_and_native_missing_reason(tmp_path: Path) -> None:
    cards = tuple(card for card in catalog(trusted_native_root=tmp_path) if card.benchmark)
    assert tuple(card.benchmark for card in cards) == ("python", "erlang", "pybeam")
    native = cards[-1]
    assert native.disabled is not None
    assert "native" in native.disabled.lower()
    assert "Raw unbounded API on current runtime" in native.explanation
    for name in ("python", "erlang"):
        config = configuration(name, tmp_path)
        assert config.native_source is None
    raw = configuration("pybeam", tmp_path)
    assert raw.native_source == str(tmp_path)
    assert raw.python_source == str(ROOT)


def test_ui__fixed_profiles_deny_parameters_and_explain_missing_native(tmp_path: Path) -> None:
    base = "http://127.0.0.1:38921"
    settings = Settings(trusted_native_root=tmp_path)
    with TestClient(create_app(settings), base_url=base) as client:
        page = client.get("/")
        assert "Matching current native build assets unavailable" in page.text
        assert "Run all available benchmarks" in page.text
        assert (
            "No command parameters"
            in client.post(
                "/start/benchmark-python?native_source=/etc", headers={"origin": base}
            ).text
        )
        assert client.post("/start/benchmark-pybeam", headers={"origin": base}).status_code == 409
