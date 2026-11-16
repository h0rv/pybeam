from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TYPECHECK_CONFIG = ["--config-file", str(ROOT / "tests/ty.toml")]
