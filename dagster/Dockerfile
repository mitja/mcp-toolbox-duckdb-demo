# Dagster image for the data-engineering track of the demo.
#
# Bundles dagster + the webserver + the dagster-dlt and dagster-dbt
# integrations + dlt's DuckDB destination + dbt-duckdb. One Python
# install for the whole EL → T → orchestration pipeline.

# Pinned to a point release so rebuilds don't silently change the base.
FROM python:3.12.13-slim

WORKDIR /app

# Build deps for any wheels that fall back to source builds.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# Pin to a recent Dagster release. Dagster's API surface (Definitions,
# dlt_assets, dbt_assets, DbtProject) is stable across the 1.10.x line.
RUN pip install --no-cache-dir \
        'dagster==1.10.16' \
        'dagster-webserver==1.10.16' \
        'dagster-dlt==0.26.16' \
        'dagster-dbt==0.26.16' \
        'dlt[duckdb,filesystem,parquet]==1.3.0' \
        'dbt-duckdb==1.9.0' \
        'setuptools<80'  # pin: setuptools 80+ removed pkg_resources, which dlt's dbt helper still imports

COPY definitions.py dlt_pipeline.py entrypoint.sh /app/
COPY dbt_project /app/dbt_project
RUN chmod +x /app/entrypoint.sh

# DAGSTER_HOME holds the run history / instance state. Mount a volume
# at this path in compose if you want runs to persist across restarts.
ENV DAGSTER_HOME=/dagster_home \
    DBT_PROFILES_DIR=/app/dbt_project \
    PYTHONUNBUFFERED=1
RUN mkdir -p "${DAGSTER_HOME}"

# /data is the shared volume that holds engineered.duckdb. dlt writes
# to it, dbt reads + writes to it, and quack-server-3 mounts it
# read-only.
VOLUME ["/data"]

EXPOSE 3000

ENTRYPOINT ["/app/entrypoint.sh"]
