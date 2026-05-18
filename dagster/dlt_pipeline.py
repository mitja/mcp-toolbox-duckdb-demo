"""dlt source for the web-events demo dataset.

Yields ~500 synthetic web-analytics events (page views and clicks)
across a few months for a handful of users in a handful of countries.
The data is deterministic — the same seed always produces the same
events — so a re-run of the pipeline is byte-stable for diffs.

The pipeline target is the **filesystem** destination — events are
written as Parquet under /data/raw/web_events/events/. Going through
Parquet rather than a shared DuckDB file avoids the file-lock
conflict that would otherwise arise between Dagster's writes and
quack-server-3 keeping its DuckDB open continuously. dbt's source
then references the Parquet via read_parquet, and the cleaner data
ends up as more Parquet files (in /data/marts/) that quack-server-3
exposes as views.

This file is intentionally a single-source dlt pipeline — production
ingest would split per-API into separate sources, but for the demo
the events come from a deterministic generator.
"""
from __future__ import annotations

import datetime as dt
import random
from typing import Iterator

import dlt

# Deterministic. Bump the seed (and the rest of the constants below)
# to refresh the demo dataset without altering the schema.
RANDOM_SEED = 20260518
NUM_EVENTS = 500
START_DATE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
END_DATE = dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc)

USERS = [f"u_{i:03d}" for i in range(1, 41)]  # 40 users
COUNTRIES = ["DE", "FR", "GB", "US", "ES", "IT", "NL"]
PAGES = [
    "/",
    "/pricing",
    "/docs",
    "/docs/getting-started",
    "/docs/api",
    "/blog",
    "/blog/launch-announcement",
    "/contact",
    "/login",
    "/signup",
]
EVENT_TYPES = ["page_view", "page_view", "page_view", "click", "scroll"]


def _generate_events() -> Iterator[dict]:
    """Deterministic event stream — seeded RNG, fixed cardinalities."""
    rng = random.Random(RANDOM_SEED)
    span_seconds = int((END_DATE - START_DATE).total_seconds())
    for i in range(NUM_EVENTS):
        ts = START_DATE + dt.timedelta(seconds=rng.randint(0, span_seconds))
        yield {
            "event_id":   i + 1,
            "ts":         ts,
            "user_id":    rng.choice(USERS),
            "country":    rng.choice(COUNTRIES),
            "page":       rng.choice(PAGES),
            "event_type": rng.choice(EVENT_TYPES),
            "session_id": f"s_{rng.randint(1, 200):04d}",
        }


@dlt.source(name="web_events")
def web_events_source():
    """dlt source — yields one resource (`events`) with the synthetic rows."""

    @dlt.resource(
        name="events",
        primary_key="event_id",
        write_disposition="replace",
    )
    def events() -> Iterator[dict]:
        yield from _generate_events()

    return events


def build_pipeline() -> dlt.Pipeline:
    """The dlt pipeline that the Dagster asset materializer drives.

    Filesystem destination writes Parquet under
    /data/raw/web_events/events/. write_disposition="replace" on the
    resource above blows away the previous snapshot on each run, so a
    rerun is idempotent.
    """
    return dlt.pipeline(
        pipeline_name="web_events",
        destination=dlt.destinations.filesystem(bucket_url="file:///data/raw"),
        dataset_name="web_events",
        progress=None,
    )


if __name__ == "__main__":
    # Useful for local iteration: `python dlt_pipeline.py`
    pipeline = build_pipeline()
    info = pipeline.run(web_events_source(), loader_file_format="parquet")
    print(info)
