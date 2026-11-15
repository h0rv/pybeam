# Source only inside an explicit Bash session with set -euo pipefail.
genstage_abort() {
    printf 'GenStage promotion aborted: %s\n' "$*" >&2
    exit 1
}

genstage_hash() {
    local actual
    actual="$(shasum -a 256 "$1")" || genstage_abort "cannot hash $1"
    [[ "${actual%% *}" == "$2" ]] || genstage_abort "hash mismatch: $1"
}

genstage_native_helper_hashes() {
    genstage_hash "$1/src/py_application.erl" 3a116f240ec2205fe36801764ddd7508b8c1f8e6fd19c67e16aa070212ce676e
    genstage_hash "$1/src/py_callback.erl" 653b0e77ad2bda9b49f7bfe1e55bd240e9475a24797fe5f1d29e518efa3f4435
    genstage_hash "$1/src/py_launch.erl" fbbdf78852d71dce78594e30d7e15c4d3adc94ca91217332822a1c2ce3aa1003
}

genstage_preflight() {
    local root="$1" native="$2" wheel="$3" reviewed_sdk="$4" reviewed_native="$5" source_nif="$6"
    local name actual status
    [[ "$-" == *e* && "$-" == *u* ]] || genstage_abort 'Bash errexit and nounset are required'
    case ":$SHELLOPTS:" in
        *:pipefail:*) ;;
        *) genstage_abort 'Bash pipefail is required' ;;
    esac
    [[ "${PYBEAM_GENSTAGE_PROMOTION_APPROVED:-}" == "$reviewed_sdk" ]] || genstage_abort 'explicit reviewed SDK approval is required'
    while IFS= read -r name; do
        [[ "$name" == PYBEAM_GENSTAGE_PROMOTION_APPROVED ]] || genstage_abort "unexpected environment option: $name"
    done < <(compgen -A variable PYBEAM_ || true)
    for name in PYTHONPATH PYTHONHOME PYTHONOPTIMIZE PYTEST_ADDOPTS PYTEST_PLUGINS DYLD_LIBRARY_PATH DYLD_FALLBACK_LIBRARY_PATH LD_LIBRARY_PATH ERL_FLAGS ERL_AFLAGS ERL_ZFLAGS; do
        if declare -p "$name" >/dev/null 2>&1; then
            genstage_abort "unexpected environment option: $name"
        fi
    done
    genstage_hash "$wheel" 75192f28d0ba1fc7e021662ba26daf50430b409267179dee50cb99ccbe9269e2
    actual="$(git -C "$root" rev-parse --show-toplevel)" || genstage_abort 'SDK is not a Git checkout'
    [[ "$actual" == "$root" ]] || genstage_abort 'SDK root differs from the explicit path'
    actual="$(git -C "$root" rev-parse HEAD)" || genstage_abort 'cannot read SDK HEAD'
    [[ "$actual" == "$reviewed_sdk" ]] || genstage_abort 'SDK HEAD is not the reviewed commit'
    status="$(git -C "$root" status --porcelain)" || genstage_abort 'cannot inspect SDK status'
    [[ -z "$status" ]] || genstage_abort 'SDK source is dirty'
    actual="$(git -C "$native" rev-parse HEAD)" || genstage_abort 'cannot read native HEAD'
    [[ "$actual" == "$reviewed_native" ]] || genstage_abort 'native HEAD is not the explicitly reviewed commit'
    status="$(git -C "$native" status --porcelain)" || genstage_abort 'cannot inspect native status'
    [[ -z "$status" ]] || genstage_abort 'native source is dirty'
    actual="$(git -C "$native" rev-parse HEAD:c_src)" || genstage_abort 'cannot read native C source tree'
    [[ "$actual" == ff743477747ef9e882a2718f665a8d339178c221 ]] || genstage_abort 'native C source tree changed'
    genstage_native_helper_hashes "$native"
    actual="$(uv run --locked python -c 'import sys,sysconfig; print(".".join(map(str,sys.version_info[:3])) + ("t" if sysconfig.get_config_var("Py_GIL_DISABLED") else ""))')" || genstage_abort 'cannot inspect selected Python'
    [[ "$actual" == 3.14.10 ]] || genstage_abort 'select the declared Python 3.14.10 through mise exec'
    local declared_python
    declared_python="$(mise where python@3.14.10)" || genstage_abort 'cannot locate the declared Python'
    actual="$(uv run --locked python -c 'import pathlib,sys; print(pathlib.Path(sys.base_prefix).resolve())')" || genstage_abort 'cannot inspect project interpreter prefix'
    [[ "$actual" == "$declared_python" ]] || genstage_abort 'project environment does not use the declared mise Python'
    actual="$(mise where erlang@29.1.2)" || genstage_abort 'cannot locate the declared OTP'
    [[ "$(<"$actual/releases/29/OTP_VERSION")" == 29.1.2 ]] || genstage_abort 'declared OTP version changed'
    genstage_hash "$source_nif" 6d80e6506fa4f347226b2b593df1dba05f224257c2292f058759c6b8f5047b1a
}

genstage_verify_generation() {
    local generation="$1" previous_runtime="$2"
    genstage_hash "$generation/provenance.json" 33752f3d805ea12c22d1f5336259e98c37717a8cfc3f049e9d6bb4094c6771ea
    uv run --locked python - "$generation" "$previous_runtime" <<'PY'
import sys
from pathlib import Path

from tools.genstage_assets import Provenance, digest
from tools.runtime_wheel import runtime_tag

root, previous = map(Path, sys.argv[1:])
proof = Provenance.model_validate_json((root / "provenance.json").read_bytes())
if len(proof.runtime_payloads) != 707 or len(proof.preserved_hornbeam_payloads) != 52:
    raise SystemExit("Unexpected prepared payload counts")
actual = {str(path.relative_to(root / "runtime")) for path in (root / "runtime").rglob("*") if path.is_file()}
if actual != set(proof.runtime_payloads):
    raise SystemExit("Prepared runtime file set changed")
for name, expected in proof.runtime_payloads.items():
    if digest(root / "runtime" / name) != expected:
        raise SystemExit("Prepared runtime changed: " + name)
for name, expected in proof.preserved_hornbeam_payloads.items():
    if digest(previous / name) != expected:
        raise SystemExit("Existing Hornbeam changed: " + name)
runtime_tag(root / "runtime")
print("VERIFIED_GENERATION 707 files; 52 preserved Hornbeam files")
PY
}
