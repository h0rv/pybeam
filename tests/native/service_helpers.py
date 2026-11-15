import sys
from importlib.machinery import ModuleSpec
from importlib.util import module_from_spec
from pathlib import Path

NATIVE = Path(__file__).resolve().parents[2].parent / "erlang-python"


def load_service_helpers() -> None:
    # Only the real pure-Python loop and service helpers are needed here.
    # Bypass the package initializer, which imports the VM-only erlang module.
    spec = ModuleSpec("_erlang_impl", loader=None, is_package=True)
    locations = spec.submodule_search_locations
    if locations is None:
        raise RuntimeError("expected a helper package specification")
    locations.append(str(NATIVE / "priv" / "_erlang_impl"))
    sys.modules["_erlang_impl"] = module_from_spec(spec)
    sys.path.append(str(NATIVE / "priv"))
