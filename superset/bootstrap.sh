#!/usr/bin/env bash
# Idempotent first-boot bootstrap for the Superset demo container.
#
# Runs `superset db upgrade` (metadata schema), creates an admin user
# if missing, registers the Cube SQL API as a database connection if
# missing, then hands off to the bootstrap.py helper which installs
# the demo dataset/chart/dashboard via Superset's REST API.
#
# Idempotent: re-runs on `docker compose up` after a stop are safe.
# The metastore lives in a Compose volume so dashboards persist
# across restarts.

set -eu

SUPERSET_ADMIN_USERNAME="${SUPERSET_ADMIN_USERNAME:-admin}"
SUPERSET_ADMIN_PASSWORD="${SUPERSET_ADMIN_PASSWORD:-admin}"
SUPERSET_ADMIN_EMAIL="${SUPERSET_ADMIN_EMAIL:-admin@example.com}"

echo "bootstrap: superset db upgrade"
superset db upgrade

# fab create-admin exits non-zero if the user already exists; allow that,
# but echo the output so a *real* failure (bad metastore, broken config)
# isn't silently swallowed.
echo "bootstrap: ensure admin user exists"
if ! create_admin_out=$(superset fab create-admin \
    --username "${SUPERSET_ADMIN_USERNAME}" \
    --firstname Admin --lastname User \
    --email "${SUPERSET_ADMIN_EMAIL}" \
    --password "${SUPERSET_ADMIN_PASSWORD}" 2>&1); then
    echo "${create_admin_out}"
    echo "bootstrap: create-admin failed (expected if the user already exists) — continuing"
fi

echo "bootstrap: superset init (permissions + default roles)"
superset init

echo "bootstrap: start gunicorn in background; wait for /health"
gunicorn \
    --bind 0.0.0.0:8088 \
    --workers 2 \
    --worker-class gthread \
    --threads 8 \
    --timeout 120 \
    "superset.app:create_app()" &
SUPERSET_PID=$!

# Wait for /health to return 200 before the bootstrap REST calls.
healthy=0
for i in $(seq 1 60); do
    if curl -sf http://127.0.0.1:8088/health > /dev/null; then
        echo "bootstrap: superset is healthy"
        healthy=1
        break
    fi
    sleep 1
done

if [ "${healthy}" -eq 1 ]; then
    # Run the API-driven dashboard bootstrap (idempotent).
    echo "bootstrap: registering Cube database + dataset + chart + dashboard"
    python3 /app/bootstrap.py || {
        echo "bootstrap: dashboard bootstrap failed — superset stays up so the user can finish via UI"
    }
else
    # Don't fire REST calls at a server that never came up; the
    # container healthcheck will flag it and gunicorn's own logs
    # (below, via wait) are the place to debug.
    echo "bootstrap: superset /health never turned green after 60s — skipping dashboard bootstrap"
fi

# Hand stdout/stderr back to gunicorn and wait on it.
wait "${SUPERSET_PID}"
