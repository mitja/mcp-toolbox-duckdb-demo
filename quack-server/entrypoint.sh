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
# Start fresh on every container start. init.sql's CREATE/INSERT statements
# are not idempotent (and shouldn't be — the demo is a 30-row sample, not a
# durable dataset), so a `docker compose restart` against a persisted
# /data/analytics.duckdb fails with primary-key collisions. Wiping the file
# at startup also makes the reconnect smoke test deterministic.
rm -f /data/analytics.duckdb /data/analytics.duckdb.wal

exec sh -c "tail -f /dev/null | duckdb /data/analytics.duckdb -init /quack/init.sql"
