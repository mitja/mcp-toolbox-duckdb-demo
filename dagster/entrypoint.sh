#!/usr/bin/env bash
# Dagster container entrypoint.
#
# 1. Run the asset graph once so /data/engineered.duckdb exists with
#    populated tables before quack-server-3 starts serving it.
# 2. Hand off to `dagster dev` which runs the webserver (port 3000)
#    and the daemon (schedules + sensors) in one process.
#
# The initial materialize is idempotent — dlt's write_disposition is
# "replace" and dbt's models are all views, so re-running blows away
# and rebuilds the data without leftover state.
#
# On success the script touches /data/.bootstrap-ok. quack-server-3's
# healthcheck requires that sentinel, so a failed dlt/dbt bootstrap
# keeps quack-server-3 unhealthy (instead of "healthy" but serving
# views over Parquet files that don't exist) while this container's
# UI stays up for debugging/retrying.

set -eu

cd /app

# Make the data directories. /data/marts is where dbt-duckdb's
# external materialization writes — dbt does not auto-create it.
mkdir -p /data /data/marts /data/raw
rm -f /data/.bootstrap-ok

# DbtProject.prepare_if_dev() only writes the manifest when running
# under `dagster dev`. For the one-shot `asset materialize` we drive
# in this script, the manifest must already exist on disk.
echo "dagster-bootstrap: dbt parse (compile manifest)"
(cd /app/dbt_project && dbt parse --quiet)

# Materialize in two steps because the dlt asset and the dbt assets
# don't share an asset key (dagster-dlt uses dataset/table names,
# dagster-dbt uses dbt's source name + schema + table). Running them
# in dependency order by hand is simpler than reconciling key shapes.
echo "dagster-bootstrap: materialize dlt ingest"
if dagster asset materialize \
    --module-name definitions \
    --select 'group:ingest'; then
  # dbt-models asset selection through the dagster CLI has version-
  # specific syntax issues with dbt-asset keys; calling dbt build
  # directly bypasses that and writes the same Parquets to /data/marts/.
  # The Dagster UI still shows the dbt assets — they just got
  # materialized outside of Dagster's run history for this initial
  # bootstrap. The 15-minute schedule will materialize them through
  # Dagster normally. `dbt build` also runs the schema tests in
  # models/schema.yml, so the sentinel below asserts data quality,
  # not just file existence.
  echo "dagster-bootstrap: materialize dbt models via dbt CLI directly"
  if (cd /app/dbt_project && dbt build); then
    touch /data/.bootstrap-ok
    echo "dagster-bootstrap: success — wrote /data/.bootstrap-ok"
  else
    echo "dagster-bootstrap: dbt build failed; quack-server-3 will stay unhealthy — retry from the UI"
  fi
else
  echo "dagster-bootstrap: dlt materialize failed; skipping dbt — quack-server-3 will stay unhealthy"
fi

echo "dagster-bootstrap: starting dagster dev on :3000"
exec dagster dev --host 0.0.0.0 --port 3000 --module-name definitions
