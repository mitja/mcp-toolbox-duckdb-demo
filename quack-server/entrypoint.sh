#!/bin/sh
# Render init.sql.tmpl with the QUACK_TOKEN env var, then start DuckDB.
# Piping /dev/null keeps the CLI alive after init completes so the Quack
# server threads (spawned by CALL quack_serve) keep accepting connections.
set -eu

: "${QUACK_TOKEN:?QUACK_TOKEN must be set}"
: "${QUACK_PORT:=9494}"

export QUACK_TOKEN QUACK_PORT
envsubst < /quack/init.sql.tmpl > /quack/init.sql

mkdir -p /data
exec sh -c "tail -f /dev/null | duckdb /data/analytics.duckdb -init /quack/init.sql"
