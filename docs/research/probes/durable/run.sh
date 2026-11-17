#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export ERL_FLAGS='+S 2:2 +A 2'
if [[ ${1:-} == node ]]; then
  exec erl -noshell -sname "$2" -setcookie durable_probe -pa _build/default/lib/*/ebin -eval "durable_probe:boot($3, \"$4\")."
fi
rebar3 compile
if [[ ${1:-} == lease ]]; then
  exec erl -noshell -sname durable_control -setcookie durable_probe -pa _build/default/lib/*/ebin -eval "durable_probe:lease($2), halt()."
fi
if [[ ${2:-} == sync_log ]]; then
  exec erl -noshell -sname durable_control -setcookie durable_probe -pa _build/default/lib/*/ebin -eval "durable_probe:run($1, true), halt()."
fi
exec erl -noshell -sname durable_control -setcookie durable_probe -pa _build/default/lib/*/ebin -eval "durable_probe:run(${1:-store_khepri}), halt()."
