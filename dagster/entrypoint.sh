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

set -eu

cd /app

# Make the data directories. /data/marts is where dbt-duckdb's
# external materialization writes — dbt does not auto-create it.
mkdir -p /data /data/marts /data/raw

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
dagster asset materialize \
    --module-name definitions \
    --select 'group:ingest' \
    || echo "dagster-bootstrap: dlt materialize failed; will skip dbt"

echo "dagster-bootstrap: materialize dbt models via dbt CLI directly"
# dbt-models asset selection through the dagster CLI has version-
# specific syntax issues with dbt-asset keys; calling dbt build
# directly bypasses that and writes the same Parquets to /data/marts/.
# The Dagster UI still shows the dbt assets — they just got
# materialized outside of Dagster's run history for this initial
# bootstrap. The 15-minute schedule will materialize them through
# Dagster normally.
(cd /app/dbt_project && dbt build) \
    || echo "dagster-bootstrap: dbt build failed; the UI stays up so you can retry from there"

echo "dagster-bootstrap: starting dagster dev on :3000"
exec dagster dev --host 0.0.0.0 --port 3000 --module-name definitions
