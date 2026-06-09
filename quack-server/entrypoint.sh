#!/bin/sh
# Render init.sql.tmpl with the QUACK_TOKEN env var, then start DuckDB.
# Piping /dev/null keeps the CLI alive after init completes so the Quack
# server threads (spawned by CALL quack_serve) keep accepting connections.
set -eu

: "${QUACK_TOKEN:?QUACK_TOKEN must be set}"
: "${QUACK_PORT:=9494}"
# Path of the served DuckDB file. quack-server-3 overrides this to a
# container-local path so the shared /data volume can be mounted :ro.
: "${QUACK_DB_PATH:=/data/analytics.duckdb}"

export QUACK_TOKEN QUACK_PORT
# Whitelist exactly the variables the template expects. A bare
# `envsubst` would substitute EVERY ${...} occurrence, silently
# blanking any future SQL that happens to contain one.
# Render to /tmp so the token-bearing init.sql doesn't linger in
# /quack for the container's lifetime.
envsubst '${QUACK_TOKEN} ${QUACK_PORT}' < /quack/init.sql.tmpl > /tmp/init.sql

db_dir=$(dirname "${QUACK_DB_PATH}")
mkdir -p "${db_dir}"
# Start fresh on every container start. init.sql's CREATE/INSERT statements
# are not idempotent (and shouldn't be — the demo is a 30-row sample, not a
# durable dataset), so a `docker compose restart` against a persisted
# DuckDB file fails with primary-key collisions. Wiping the file
# at startup also makes the reconnect smoke test deterministic.
rm -f "${QUACK_DB_PATH}" "${QUACK_DB_PATH}.wal"

exec sh -c "tail -f /dev/null | duckdb '${QUACK_DB_PATH}' -init /tmp/init.sql"
