#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$ROOT"
if [[ "${PYBEAM_ATTACH_MISE:-}" != 1 ]]; then
    exec mise exec -- env PYBEAM_ATTACH_MISE=1 bash "$ROOT/docs/research/probes/attach/run.sh"
fi
PYTHON="$(uv run --locked python -c 'import sys; print(sys.executable)')"
COOKIE="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(32))')"
export ERL_EPMD_ADDRESS=127.0.0.1
export ERL_EPMD_PORT
ERL_EPMD_PORT="$("$PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
trap 'epmd -kill >/dev/null 2>&1 || true' EXIT
export PYTHONHOME
PYTHONHOME="$("$PYTHON" -c 'import sys; print(sys.base_prefix)')"
export LD_LIBRARY_PATH="$PYTHONHOME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH="$ROOT/src:$ROOT/docs/research/probes/attach:$ROOT/_build/hornbeam/lib/hornbeam/priv:$("$PYTHON" -c 'import site; print(":".join(site.getsitepackages()))')"
unset ERL_FLAGS PYBEAM_BOOTSTRAP PYBEAM_HORNBEAM_SERVICES PYBEAM_DISTRIBUTION PYBEAM_CLUSTER
export ERL_CRASH_DUMP="$ROOT/_build/attach-erl-crash.dump"
paths=("$ROOT"/_build/hornbeam/lib/*/ebin "$ROOT"/_build/hornbeam/checkouts/*/ebin)
ebins=()
for path in "${paths[@]}"; do
    [[ ! -d "$path" ]] || ebins+=("$path")
done
erl +S 2:2 +SDcpu 2 +SDio 2 -noshell -sname host -setcookie "$COOKIE" \
    -kernel inet_dist_use_interface '{127,0,0,1}' \
    -pa "${ebins[@]}" -eval "$(< "$ROOT/docs/research/probes/attach/host.eval")"
