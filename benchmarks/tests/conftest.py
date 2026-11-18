import os
from pathlib import Path

import pytest


@pytest.fixture
def native_source() -> Path:
    configured = os.environ.get("PYBEAM_BENCHMARK_NATIVE_ROOT")
    if configured is not None:
        return Path(configured).resolve(strict=True)
    return Path(__file__).resolve().parents[2] / "_build/hornbeam/lib/erlang_python"
