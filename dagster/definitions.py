"""Dagster Definitions wiring the web-events EL→T pipeline.

Asset graph:

    web_events_source.events   (dlt)
        └──► stg_events        (dbt view)
              ├──► daily_traffic     (dbt view)
              └──► top_pages_30d     (dbt view)

dlt runs first to populate raw.events; dbt then transforms it into
the analytics schema. dagster-dbt + dagster-dlt expose both as
first-class assets so the dependency edge above is wired
automatically by the framework (it reads the dbt manifest's source
references and matches them to the dlt asset key).

A 15-minute schedule re-materializes the whole graph. The window is
intentionally short so the user can see Dagster's "last run" update
during a single demo session; bump it for any real deployment.
"""
from __future__ import annotations

import pathlib

from dagster import (
    Definitions,
    ScheduleDefinition,
    define_asset_job,
)
from dagster_dlt import DagsterDltResource, dlt_assets
from dagster_dbt import DbtCliResource, dbt_assets, DbtProject

from dlt_pipeline import build_pipeline, web_events_source

DBT_PROJECT_DIR = pathlib.Path(__file__).parent / "dbt_project"


# DbtProject.prepare_if_dev() compiles the manifest at import time
# during development; in our container we always have time to compile
# fresh since the project is tiny.
dbt_project = DbtProject(
    project_dir=DBT_PROJECT_DIR,
    profiles_dir=DBT_PROJECT_DIR,
)
dbt_project.prepare_if_dev()


# NOTE on context type hints: dagster-dlt 0.26 and dagster-dbt 0.26
# both ship their own context-type-hint validators that don't
# recognise dagster.AssetExecutionContext from the parent package
# under all import orders. Leaving `context` un-annotated bypasses
# the check; the runtime behavior is identical.
@dlt_assets(
    dlt_source=web_events_source(),
    dlt_pipeline=build_pipeline(),
    name="web_events_ingest",
    group_name="ingest",
)
def web_events_assets(context, dlt: DagsterDltResource):
    """Materialize the raw.events table from the synthetic source.

    loader_file_format="parquet" pins dlt's filesystem output to
    Parquet — the default is gzipped JSONL written as `*.jsonl`,
    which DuckDB can't read directly without an explicit compression
    hint. Parquet is the clearer demo shape anyway.
    """
    yield from dlt.run(context=context, loader_file_format="parquet")


@dbt_assets(manifest=dbt_project.manifest_path, name="dbt_models")
def dbt_models(context, dbt: DbtCliResource):
    """dbt build — runs stg_events + daily_traffic + top_pages_30d."""
    yield from dbt.cli(["build"], context=context).stream()


# One job that materializes everything. The cron schedule fires every
# 15 minutes; we also auto-materialize on container start so the
# engineered.duckdb has tables when quack-server-3 comes up.
all_assets_job = define_asset_job(
    name="materialize_all",
    selection="*",
    description="Full ingest + transform — runs dlt then dbt.",
)

every_15_minutes = ScheduleDefinition(
    name="every_15_minutes",
    job=all_assets_job,
    cron_schedule="*/15 * * * *",
)


defs = Definitions(
    assets=[web_events_assets, dbt_models],
    jobs=[all_assets_job],
    schedules=[every_15_minutes],
    resources={
        "dlt": DagsterDltResource(),
        "dbt": DbtCliResource(project_dir=dbt_project),
    },
)
