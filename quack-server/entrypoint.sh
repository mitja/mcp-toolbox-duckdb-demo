#!/bin/sh
# Render init.sql.tmpl with the QUACK_TOKEN env var, then start DuckDB with the
# rendered init on stdin. Keeping stdin open keeps the CLI alive after init
# completes so the Quack server threads (spawned by CALL quack_serve) keep
# accepting connections.
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
# The rendered init (it holds the token) goes to DuckDB on stdin and is
# never written to a file a query could read.
init=$(envsubst '${QUACK_TOKEN} ${QUACK_PORT}' < /quack/init.sql.tmpl)
unset QUACK_TOKEN

db_dir=$(dirname "${QUACK_DB_PATH}")
mkdir -p "${db_dir}"
# Start fresh on every container start. init.sql's CREATE/INSERT statements
# are not idempotent (and shouldn't be — the demo is a 30-row sample, not a
# durable dataset), so a `docker compose restart` against a persisted
# DuckDB file fails with primary-key collisions. Wiping the file
# at startup also makes the reconnect smoke test deterministic.
rm -f "${QUACK_DB_PATH}" "${QUACK_DB_PATH}.wal"

# DuckDB's CLI keeps serving while its stdin stays open (the in-process Quack
# server threads outlive the init). sh stays PID 1 and passes a stop on to it,
# so `docker compose stop` does not wait 10 s for a SIGKILL. -bail: a failing
# init statement (the authorization callback above all) stops the server
# instead of serving without it.
{ printf '%s\n' "$init"; exec tail -f /dev/null; } | duckdb -bail "${QUACK_DB_PATH}" &
pid=$!
trap 'kill "$pid" 2>/dev/null; wait "$pid"; exit 0' TERM INT
wait "$pid"
