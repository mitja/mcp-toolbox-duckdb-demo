"""Superset configuration overrides for the MCP Toolbox demo.

Intentionally minimal — the only deviations from upstream defaults are
the ones needed to run as a localhost demo container alongside the rest
of the Compose stack. Override anything here when adopting Superset
for real (rotate SECRET_KEY out of source control, swap SQLite for
Postgres metastore, set up real auth, etc.).
"""
import os

# SECRET_KEY signs the session cookie and Flask-WTF CSRF tokens.
# Stable across container restarts so existing sessions don't get
# invalidated on `docker compose restart superset`. Override via the
# SUPERSET_SECRET_KEY env var; the literal default below is fine for
# a localhost demo but should NEVER reach a public deployment.
SECRET_KEY = os.environ.get(
    "SUPERSET_SECRET_KEY", "demo-not-a-secret-rotate-me-before-prod"
)

# Disable Talisman (HTTP security headers) so the localhost demo
# doesn't redirect /health and friends through HTTPS, and so the
# Cube Playground / Toolbox UI iframes (if anyone embeds them)
# aren't blocked by frame-ancestors. Production deployments should
# leave this on.
TALISMAN_ENABLED = False

# Wider per-row limits so a Cube query can return all sample rows
# without being clipped. The data set is tiny (~30 rows) and the
# demo is read-only; in production tighten this against your warehouse.
ROW_LIMIT = 1_000
SAMPLES_ROW_LIMIT = 1_000

# Feature flags — enable a few quality-of-life things for the demo.
FEATURE_FLAGS = {
    "DASHBOARD_RBAC": False,
    "ENABLE_TEMPLATE_PROCESSING": True,
}

# WTF_CSRF_ENABLED stays on (default). Bootstrap scripts read the
# token from /login first; see superset/bootstrap.py.
