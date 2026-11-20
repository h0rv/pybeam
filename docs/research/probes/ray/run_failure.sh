#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ENVS="${PROBE_ENVS:-$HOME/probe-env}"
OUT="${1:-$HOME/probe-env/ray-failure-$(date +%s)}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
unset PYTHONPATH PYTHONHOME PROBE_ROOT_PID
docker image inspect redis:7 >/dev/null 2>&1 || {
    docker pull mirror.gcr.io/library/redis:7
    docker tag mirror.gcr.io/library/redis:7 redis:7
} >&2
REDIS="pybeam-ray-failure-$$"
docker run --detach --rm --name "$REDIS" --publish 127.0.0.1::6379 \
    redis:7 redis-server --save '' --appendonly no >/dev/null
trap 'docker rm --force "$REDIS" >/dev/null' EXIT
PORT="$(docker port "$REDIS" 6379/tcp)"
export PROBE_REDIS_URL="redis://127.0.0.1:${PORT##*:}/0"
until docker exec "$REDIS" redis-cli ping >/dev/null; do sleep 0.1; done
: > "$OUT/results.jsonl"
for trial in 1 2 3; do
    for framework in pybeam ray; do
        work="$OUT/actor-$framework-$trial"
        mkdir "$work"
        (cd "$work" && PROBE_TRIAL="$trial" timeout --kill-after=10s 60s \
            "$ENVS/${framework}314/bin/python" "$HERE/actor_$framework.py") \
            > "$work/stdout.log" 2> "$work/stderr.log"
        sed -n '/^{"probe":/p' "$work/stdout.log" > "$work/result.json"
        cat "$work/result.json" | tee -a "$OUT/results.jsonl"
    done
    for framework in pybeam celery ray; do
        timeout --kill-after=10s 115s "$ENVS/chart314/bin/python" \
            "$HERE/failure_probe.py" "$framework" "$OUT/durable-$framework-$trial" \
            --envs "$ENVS" --trial "$trial" | tee -a "$OUT/results.jsonl"
    done
done
