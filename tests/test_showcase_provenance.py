from pathlib import Path

import pytest

from benchmarks.contract import Config
from benchmarks.provenance import capture, source
from examples.showcase.catalog import ROOT


def test_python_provenance__does_not_invent_a_native_checkout() -> None:
    config = Config(python_source=str(ROOT), native_source=None)
    provenance = capture(config.python_source, config.native_source)
    assert len(provenance.sources) == 1
    assert provenance.sources[0].path == str(ROOT)


def test_native_provenance__requires_an_explicit_checkout() -> None:
    with pytest.raises(ValueError, match="explicitly configured native source"):
        Config(backend="pybeam", adapter_command=("unused",), python_source=str(ROOT))


def test_source_archive__explains_missing_git_instead_of_inventing_a_revision(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match=r"source archives without \.git are not supported"):
        source(tmp_path)
