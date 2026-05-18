"""Idempotent bootstrap of the Superset demo: Cube DB + dataset + chart + dashboard.

Called from bootstrap.sh after `superset init` and after gunicorn's
/health turns green. Talks to Superset's REST API at
http://127.0.0.1:8088 with the admin credentials seeded by the shell
wrapper. Every step checks "does this already exist?" first so a
re-run on a previously-bootstrapped metastore is a no-op.

Why not the import-zip path? Superset's YAML/zip schema is exact and
version-coupled; the API path is far more forgiving and Superset
itself validates each request. For one chart + one dashboard the API
is shorter and easier to evolve.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

import requests

SUPERSET_URL = "http://127.0.0.1:8088"
USERNAME = os.environ.get("SUPERSET_ADMIN_USERNAME", "admin")
PASSWORD = os.environ.get("SUPERSET_ADMIN_PASSWORD", "admin")

# The Cube SQL API runs inside the same Docker network as Superset.
# user/password are set on the Cube service via CUBEJS_SQL_USER /
# CUBEJS_SQL_PASSWORD; the database name is arbitrary, Cube ignores
# it. SQLAlchemy URL — Superset's preferred shape.
CUBE_SQLALCHEMY_URI = "postgresql+psycopg2://cube:cube@cube:15432/db"
CUBE_DB_NAME = "Cube (semantic layer)"

# Dataset over Cube's sales cube. Cube's SQL API exposes each cube as
# a table in the "public" schema (Cube's default).
DATASET_SCHEMA = "public"
# Cube exposes each cube as a table whose name matches the cube
# (`sales`, `orders`). Superset uses `table_name` directly as the
# SQL FROM identifier, so we keep it bare — display labels are
# handled via the dataset's verbose_name fields elsewhere.
DATASET_TABLE = "sales"

CHART_NAME = "Revenue by customer (Cube)"
DASHBOARD_TITLE = "MCP Toolbox demo — Cube semantic layer"


class Superset:
    """Thin wrapper around Superset's REST API, handling login + CSRF."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url
        self.session = requests.Session()
        # Login -> JWT access token via /api/v1/security/login.
        resp = self.session.post(
            f"{base_url}/api/v1/security/login",
            json={
                "username": username,
                "password": password,
                "provider": "db",
                "refresh": True,
            },
            timeout=20,
        )
        resp.raise_for_status()
        self.access_token = resp.json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {self.access_token}"
        # CSRF token for state-changing requests.
        csrf_resp = self.session.get(f"{base_url}/api/v1/security/csrf_token/", timeout=20)
        csrf_resp.raise_for_status()
        self.csrf_token = csrf_resp.json()["result"]
        self.session.headers["X-CSRFToken"] = self.csrf_token
        # Referer is required by Superset's CSRF guard.
        self.session.headers["Referer"] = base_url

    def get(self, path: str, **kw: Any) -> requests.Response:
        return self.session.get(f"{self.base_url}{path}", timeout=30, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> requests.Response:
        return self.session.post(f"{self.base_url}{path}", json=json, timeout=30, **kw)


def wait_for_superset() -> None:
    """Belt-and-suspenders wait on /health — bootstrap.sh already waited."""
    for _ in range(30):
        try:
            if requests.get(f"{SUPERSET_URL}/health", timeout=3).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise SystemExit("bootstrap: superset never reached /health")


def find_db_by_name(s: Superset, name: str) -> dict | None:
    """List databases and return the one whose database_name matches."""
    r = s.get("/api/v1/database/", params={"q": "(page_size:100)"})
    r.raise_for_status()
    for row in r.json().get("result", []):
        if row["database_name"] == name:
            return row
    return None


def ensure_database(s: Superset) -> dict:
    existing = find_db_by_name(s, CUBE_DB_NAME)
    if existing:
        print(f"bootstrap: database {CUBE_DB_NAME!r} already registered (id={existing['id']})")
        return existing
    payload = {
        "database_name": CUBE_DB_NAME,
        "sqlalchemy_uri": CUBE_SQLALCHEMY_URI,
        "expose_in_sqllab": True,
        "allow_ctas": False,
        "allow_cvas": False,
        "allow_dml": False,
        "allow_run_async": False,
        "extra": (
            '{"engine_params":{"connect_args":{"application_name":"superset-demo"}},'
            '"metadata_params":{},"schemas_allowed_for_file_upload":[]}'
        ),
    }
    r = s.post("/api/v1/database/", json=payload)
    if not r.ok:
        raise SystemExit(f"bootstrap: register database failed: {r.status_code} {r.text}")
    obj = r.json()
    print(f"bootstrap: registered database (id={obj['id']})")
    return obj


def find_dataset_by_name(s: Superset, name: str) -> dict | None:
    r = s.get("/api/v1/dataset/", params={"q": "(page_size:200)"})
    r.raise_for_status()
    for row in r.json().get("result", []):
        if row["table_name"] == name:
            return row
    return None


def ensure_dataset_metrics(s: Superset, dataset_id: int) -> None:
    """Idempotent: declare Superset metrics that translate to Cube measures.

    Cube exposes each measure as a column in the SQL view of the cube
    (so `revenue`, `order_count`, `avg_order_value` are all columns of
    the `sales` cube). Superset auto-introspects those columns, but
    it doesn't know they're already pre-aggregated — without a saved
    metric, a chart tries to SUM/AVG/etc. the column, which Cube
    rejects.

    The fix is a Superset saved metric whose SQL expression is
    `MEASURE(<cube_measure_name>)` — Cube's idiomatic "expose this
    column as the cube's measure". Superset then substitutes the
    metric verbatim into the SELECT clause, no double-aggregation.
    """
    r = s.get(f"/api/v1/dataset/{dataset_id}")
    r.raise_for_status()
    existing_metrics = {
        m["metric_name"] for m in r.json().get("result", {}).get("metrics", []) or []
    }
    desired = [
        {
            "metric_name": "revenue",
            "expression": "MEASURE(revenue)",
            "verbose_name": "Revenue (Cube measure)",
            "description": "Sum of sale amounts — pre-aggregated by Cube.",
        },
        {
            "metric_name": "order_count",
            "expression": "MEASURE(order_count)",
            "verbose_name": "Order count (Cube measure)",
            "description": "Number of sales orders — pre-aggregated by Cube.",
        },
    ]
    to_add = [m for m in desired if m["metric_name"] not in existing_metrics]
    if not to_add:
        return
    # Superset's PUT /dataset/<id> appends to the metrics relation
    # rather than replacing it (and rejects re-sending an existing
    # metric_name). Send only the new entries.
    r2 = s.session.put(
        f"{SUPERSET_URL}/api/v1/dataset/{dataset_id}",
        json={"metrics": to_add},
        timeout=20,
    )
    if not r2.ok:
        print(f"bootstrap: dataset metrics declare failed (non-fatal): {r2.status_code} {r2.text}")
    else:
        print(f"bootstrap: declared {len(to_add)} dataset metric(s)")


def ensure_dataset(s: Superset, db: dict) -> dict:
    # The dataset's `table_name` is used verbatim as the SQL FROM
    # identifier, so it must match the physical table on Cube
    # (`sales`). No rename — friendly labels live on metrics /
    # column verbose_names instead.
    existing = find_dataset_by_name(s, DATASET_TABLE)
    if existing:
        print(f"bootstrap: dataset {DATASET_TABLE!r} already exists (id={existing['id']})")
        ensure_dataset_metrics(s, existing["id"])
        return existing
    payload = {
        "database": db["id"],
        "schema": DATASET_SCHEMA,
        "table_name": DATASET_TABLE,
        "external_url": None,
    }
    r = s.post("/api/v1/dataset/", json=payload)
    if not r.ok:
        raise SystemExit(f"bootstrap: create dataset failed: {r.status_code} {r.text}")
    obj = r.json()
    dataset_id = obj["id"]
    ensure_dataset_metrics(s, dataset_id)
    print(f"bootstrap: created dataset (id={dataset_id})")
    return {"id": dataset_id, **obj.get("result", {})}


def ensure_chart(s: Superset, dataset_id: int) -> dict:
    r = s.get("/api/v1/chart/", params={"q": "(page_size:200)"})
    r.raise_for_status()
    for row in r.json().get("result", []):
        if row["slice_name"] == CHART_NAME:
            print(f"bootstrap: chart {CHART_NAME!r} already exists (id={row['id']})")
            return row
    # Chart params encode the column/metric selection. Use the
    # categorical bar viz (dist_bar) — `customer` goes on the x-axis
    # via groupby (NOT x_axis, which the time-series bar viz uses
    # and which conflicts when the column also appears in groupby:
    # "Duplicate column/metric labels"). Y-axis is the saved metric
    # `revenue` which expands to MEASURE(revenue) on Cube's pg API
    # (see ensure_dataset_metrics).
    chart_params = {
        "viz_type": "dist_bar",
        "datasource": f"{dataset_id}__table",
        "metrics": ["revenue"],
        "groupby": ["customer"],
        "row_limit": 1000,
        "show_legend": False,
        "show_bar_value": True,
        "order_desc": True,
        "color_scheme": "supersetColors",
    }
    import json as _json
    payload = {
        "slice_name": CHART_NAME,
        "viz_type": chart_params["viz_type"],
        "datasource_id": dataset_id,
        "datasource_type": "table",
        "params": _json.dumps(chart_params),
    }
    r = s.post("/api/v1/chart/", json=payload)
    if not r.ok:
        raise SystemExit(f"bootstrap: create chart failed: {r.status_code} {r.text}")
    obj = r.json()
    print(f"bootstrap: created chart (id={obj['id']})")
    return obj


def link_chart_to_dashboard(s: Superset, chart_id: int, dashboard_id: int) -> None:
    """Idempotent: PUT the chart with the dashboard in its `dashboards` array.

    Linking the chart to the dashboard requires *both* sides of the
    relation in Superset 4.x: position_json on the dashboard for the
    spatial layout AND the chart's `dashboards` array for the
    chart→dashboard back-reference. Without the latter the dashboard
    renders empty even though position_json references the chart.
    """
    r = s.session.put(
        f"{SUPERSET_URL}/api/v1/chart/{chart_id}",
        json={"dashboards": [dashboard_id]},
        timeout=20,
    )
    if not r.ok:
        print(f"bootstrap: chart-dashboard link failed (non-fatal): {r.status_code} {r.text}")


def ensure_dashboard(s: Superset, chart_id: int) -> dict:
    r = s.get("/api/v1/dashboard/", params={"q": "(page_size:200)"})
    r.raise_for_status()
    for row in r.json().get("result", []):
        if row["dashboard_title"] == DASHBOARD_TITLE:
            print(f"bootstrap: dashboard {DASHBOARD_TITLE!r} already exists (id={row['id']})")
            # Always re-link in case a previous run created the
            # dashboard but failed before the chart was attached.
            link_chart_to_dashboard(s, chart_id, row["id"])
            return row
    payload = {
        "dashboard_title": DASHBOARD_TITLE,
        "slug": "mcp-toolbox-cube-demo",
        "published": True,
    }
    r = s.post("/api/v1/dashboard/", json=payload)
    if not r.ok:
        raise SystemExit(f"bootstrap: create dashboard failed: {r.status_code} {r.text}")
    obj = r.json()
    dashboard_id = obj["id"]
    # Attach the chart by writing the dashboard position_json. Superset
    # arranges charts in a tree of CHART/ROW/GRID nodes; this is the
    # minimum-viable single-chart layout.
    import json as _json
    chart_node_id = f"CHART-{chart_id}"
    position_json = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {
            "type": "GRID",
            "id": "GRID_ID",
            "children": ["ROW-1"],
            "parents": ["ROOT_ID"],
        },
        "ROW-1": {
            "type": "ROW",
            "id": "ROW-1",
            "children": [chart_node_id],
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        },
        chart_node_id: {
            "type": "CHART",
            "id": chart_node_id,
            "children": [],
            "parents": ["ROOT_ID", "GRID_ID", "ROW-1"],
            "meta": {
                "width": 12,
                "height": 50,
                "chartId": chart_id,
                "sliceName": CHART_NAME,
            },
        },
    }
    update = {"position_json": _json.dumps(position_json)}
    r2 = s.session.put(
        f"{SUPERSET_URL}/api/v1/dashboard/{dashboard_id}", json=update, timeout=20
    )
    if not r2.ok:
        print(f"bootstrap: dashboard layout update failed (non-fatal): {r2.status_code} {r2.text}")
    link_chart_to_dashboard(s, chart_id, dashboard_id)
    print(f"bootstrap: created dashboard (id={dashboard_id})")
    return obj


def main() -> int:
    wait_for_superset()
    try:
        s = Superset(SUPERSET_URL, USERNAME, PASSWORD)
    except Exception as exc:
        print(f"bootstrap: failed to authenticate against Superset: {exc}", file=sys.stderr)
        return 1
    db = ensure_database(s)
    ds = ensure_dataset(s, db)
    chart = ensure_chart(s, ds["id"])
    ensure_dashboard(s, chart["id"])
    print("bootstrap: all done. Open http://localhost:8088 (admin/admin) → Dashboards.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
