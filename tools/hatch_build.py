import os
from pathlib import Path
from typing import override

from hatchling.builders.config import BuilderConfig
from hatchling.builders.hooks.plugin.interface import BuildHookInterface

from tools.runtime_wheel import runtime_tag


class RuntimeBuildHook(BuildHookInterface[BuilderConfig]):
    @override
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if self.target_name != "wheel" or version == "editable":
            return
        assets = (
            Path(self.root) / os.environ.get("PYBEAM_BUILD_RUNTIME", "build/runtime")
        ).resolve()
        build_data["tag"] = runtime_tag(assets)
        build_data["pure_python"] = False
        priv = next(assets.glob("lib/erlang_python-*/priv"))
        build_data["force_include"] = {
            str(assets): "pybeam/runtime/_assets",
            str(priv / "_erlang_impl"): "_erlang_impl",
            str(priv / "_erlang_isolated"): "_erlang_isolated",
            str(priv / "erlang-stubs"): "erlang-stubs",
            str(priv / "erlang_services.py"): "erlang_services.py",
            str(priv / "py_isolated_child.py"): "py_isolated_child.py",
        }
