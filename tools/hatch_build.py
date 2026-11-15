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
        assets = Path(self.root) / "build" / "runtime"
        build_data["tag"] = runtime_tag(assets)
        build_data["pure_python"] = False
        build_data["force_include"] = {str(assets): "pybeam/runtime/_assets"}
