#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENVS="${PROBE_ENVS:-$HOME/probe-env}"
OUT="${1:-$HOME/probe-env/ray-comparison}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
unset PYTHONPATH PYTHONHOME PROBE_ROOT_PID
cd "$OUT"
: > results.jsonl
for trial in 1 2 3; do
    for framework in pybeam ray; do
        PROBE_TRIAL="$trial" timeout --signal=TERM --kill-after=30s 600s \
            "$ENVS/${framework}314/bin/python" "$HERE/${framework}_probe.py" \
            > "$framework-$trial.json" 2> "$framework-$trial.log"
        cat "$framework-$trial.json" | tee -a results.jsonl
    done
done
"$ENVS/chart314/bin/python" "$HERE/chart.py" results.jsonl comparison.png
