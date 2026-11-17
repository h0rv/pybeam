#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export ERL_FLAGS='+S 2:2 +A 2'
if [[ ${1:-} == node ]]; then
  exec erl -noshell -sname "$2" -setcookie durable_probe -pa _build/default/lib/*/ebin -eval "durable_probe:boot($3, \"$4\")."
fi
rebar3 compile
if [[ ${1:-} == witness ]]; then
  exec erl -noshell -sname log_order_control -setcookie durable_probe -pa _build/default/lib/*/ebin -eval "log_order_probe:run(${2:-single}), halt()."
fi
if [[ ${1:-} == matrix ]]; then
  exec erl -noshell -sname matrix_control -setcookie durable_probe -pa _build/default/lib/*/ebin -eval "crash_matrix:run(${2:-store_mnesia_synced}, ${3:-1}, ${4:-5}), halt()."
fi
if [[ ${1:-} == lease ]]; then
  exec erl -noshell -sname durable_control -setcookie durable_probe -pa _build/default/lib/*/ebin -eval "durable_probe:lease($2), halt()."
fi
if [[ ${2:-} == sync_log ]]; then
  exec erl -noshell -sname durable_control -setcookie durable_probe -pa _build/default/lib/*/ebin -eval "durable_probe:run($1, true), halt()."
fi
exec erl -noshell -sname durable_control -setcookie durable_probe -pa _build/default/lib/*/ebin -eval "durable_probe:run(${1:-store_khepri}), halt()."
