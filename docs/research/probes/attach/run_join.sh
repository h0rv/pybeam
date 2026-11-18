#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
cd "$ROOT"
if [[ "${PYBEAM_JOIN_MISE:-}" != 1 ]]; then
    exec mise exec -- env PYBEAM_JOIN_MISE=1 bash "$ROOT/docs/research/probes/attach/run_join.sh"
fi
export PROBE_PYTHON PROBE_DIRECTORY PROBE_ROOT PROBE_WORKDIR
PROBE_ROOT="$ROOT"
PROBE_WORKDIR="$ROOT/_build/join-probe"
PROBE_PYTHON="$(uv run --locked python -c 'import sys; print(sys.executable)')"
COOKIE="$("$PROBE_PYTHON" -c 'import secrets; print(secrets.token_hex(32))')"
export ERL_EPMD_ADDRESS=127.0.0.1
export ERL_EPMD_PORT
ERL_EPMD_PORT="$("$PROBE_PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
trap 'epmd -kill >/dev/null 2>&1 || true' EXIT
PROBE_DIRECTORY="$ROOT/docs/research/probes/attach"
export PYTHONHOME
PYTHONHOME="$("$PROBE_PYTHON" -c 'import sys; print(sys.base_prefix)')"
export LD_LIBRARY_PATH="$PYTHONHOME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH="$ROOT/src:$PROBE_DIRECTORY:$ROOT/_build/hornbeam/lib/hornbeam/priv:$("$PROBE_PYTHON" -c 'import site; print(":".join(site.getsitepackages()))')"
unset ERL_FLAGS PYBEAM_BOOTSTRAP PYBEAM_HORNBEAM_SERVICES PYBEAM_DISTRIBUTION PYBEAM_CLUSTER
export ERL_CRASH_DUMP="$ROOT/_build/join-erl-crash.dump"
mkdir -p "$ROOT/_build/join-probe"
erlc -Werror -o "$ROOT/_build/join-probe" "$PROBE_DIRECTORY/join_probe.erl"
ebins=("$ROOT/_build/join-probe")
for path in "$ROOT"/_build/hornbeam/lib/*/ebin "$ROOT"/_build/hornbeam/checkouts/*/ebin; do
    [[ ! -d "$path" ]] || ebins+=("$path")
done
export PROBE_EBINS
PROBE_EBINS="$(IFS=:; echo "${ebins[*]}")"
erl +S 2:2 +SDcpu 2 +SDio 2 -noshell -sname host -setcookie "$COOKIE" \
    -kernel inet_dist_use_interface '{127,0,0,1}' \
    -pa "$ROOT/_build/join-probe" -s join_probe run
