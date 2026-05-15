# mcp-toolbox-duckdb-demo

End-to-end demo stack for the **MCP Toolbox DuckDB / Quack adapter** that
lives in the sibling fork [`mitja/mcp-toolbox-duckdb`](https://github.com/mitja/mcp-toolbox-duckdb)
(branch `feat/duckdb-quack`). The stack:

```
Claude Code / LangGraph
        │
        ▼
   MCP Toolbox  ──── duckdb-sql tool ────▶ DuckDB (in-process client)
   (port 5000)                                       │
        │                                            │ ATTACH 'quack:...'
        ▼                                            ▼
       /mcp                                  Quack remote (port 9494)
                                                   ▲
                                                   │ token + read-only authz
                                              docker container
```

## Prerequisites

- Docker + Compose v2 (`docker compose ...`, not `docker-compose`).
- A local clone of the [`mcp-toolbox-duckdb`](https://github.com/mitja/mcp-toolbox-duckdb)
  fork as a **sibling directory** (so `../mcp-toolbox-duckdb` resolves from
  this repo). The Compose file builds Toolbox from that directory.
- An Anthropic API key, only if you want to run the LangGraph agent demo.

## Quickstart

```bash
cp .env.example .env
$EDITOR .env                    # set QUACK_TOKEN (and ANTHROPIC_API_KEY for the agent)

docker compose up --build       # builds and starts quack + toolbox
```

> **Prefer an interactive walkthrough?** Open
> [`notebooks/walkthrough.ipynb`](notebooks/walkthrough.ipynb) in
> Jupyter. It drives the whole demo in 38 cells — `.env` setup,
> Compose up, every toolset (curated queries, metadata discovery,
> dev-only execute-sql), the reconnect path, distributed tracing
> through Jaeger, the optional LangGraph agent, and the Claude
> Code MCP config — and tears the stack down at the end. Needs
> `requests` (already present in any standard Jupyter install).

When `toolbox-duckdb-1` logs `Server ready to serve` (or similar), the MCP
Toolbox is reachable on `localhost:5555`. Smoke-test the tool:

```bash
# List the default toolset (or any specific one)
curl -s http://localhost:5555/api/toolset | jq .

# Invoke the curated revenue tool
curl -s -X POST http://localhost:5555/api/tool/revenue_by_customer/invoke \
    -H 'Content-Type: application/json' \
    -d '{"customer_pattern": "gmbh"}' \
    | jq '.result | fromjson'
```

You should get back a JSON response shaped like spec §7: typed columns,
ordered rows, row count, truncation flag, and a statement hash. The
Toolbox `/api/tool/<name>/invoke` endpoint wraps the response in a
`{"result": "<json-string>"}` envelope, so the `jq '.result | fromjson'`
unwraps it. Example output:

```json
{
  "columns": [
    {"name": "customer", "type": "VARCHAR"},
    {"name": "revenue",  "type": "DECIMAL(38,2)"},
    {"name": "orders",   "type": "BIGINT"}
  ],
  "rows": [
    {"customer": "Alice GmbH", "revenue": "2661.65", "orders": 4},
    {"customer": "Frank GmbH", "revenue": "410",     "orders": 1}
  ],
  "row_count": 2,
  "truncated": false,
  "source": "sales-quack",
  "statement_hash": "sha256:..."
}
```

## Metadata tools

The demo `tools.yaml` exposes two toolsets:

- **`analytics_readonly`** — `revenue_by_customer`, `top_products`. The
  curated, parameterized queries an agent uses to answer questions
  about the data.
- **`analytics_metadata`** — `list_catalogs`, `list_remote_schemas`,
  `list_remote_tables`, `describe_sales`, `describe_orders`,
  `summarize_sales`. The discovery tools an agent uses to learn the
  catalog before constructing a query. All six are parameterless from
  the agent's perspective — schema/table scope is baked into
  `tools.yaml` so deployment-time RBAC, not runtime tool calls,
  controls what the agent can see.

Smoke-test the metadata tools through the HTTP API:

```bash
# List the toolset's contents
curl -s http://localhost:5555/api/toolset/analytics_metadata | jq '{tools: (.tools | keys)}'

# Discovery flow: catalogs -> schemas -> tables -> describe a table
curl -s -X POST http://localhost:5555/api/tool/list_catalogs/invoke      -d '{}' | jq '.result | fromjson'
curl -s -X POST http://localhost:5555/api/tool/list_remote_tables/invoke -d '{}' | jq '.result | fromjson'
curl -s -X POST http://localhost:5555/api/tool/describe_sales/invoke     -d '{}' | jq '.result | fromjson'

# Per-column statistics
curl -s -X POST http://localhost:5555/api/tool/summarize_sales/invoke    -d '{}' | jq '.result | fromjson'
```

The metadata tools that target the remote DuckDB (everything except
`list_catalogs`) push their SQL through Quack's `quack_query()` table
function. The Toolbox-side `information_schema` view of an ATTACHed
catalog is intentionally incomplete (DuckDB does not push catalog
enumeration through ATTACH), so `quack_query()` is the route that
sees the live remote schema.

## Development-only ad-hoc SQL (`analytics_dev`)

The demo also exposes a third toolset, `analytics_dev`, with a single
tool: `dev_duckdb_execute_sql`. This is a **dev-only** surface —
intended for local exploration and human-in-the-loop debugging, **not
for production agents** (spec §3 explicitly classifies a "let the LLM
run arbitrary SQL" surface as a non-goal).

The tool is gated behind `enabled: true` in `tools.yaml`: Toolbox
refuses to start unless that field is explicitly present and true,
and a WARN line is emitted to the container logs on every boot:

```text
WARN duckdb-execute-sql is enabled. This tool exposes an
agent-supplied SQL surface and is intended for local development
and human-in-the-loop debugging only; do not enable it for
production agent toolsets. tool=dev_duckdb_execute_sql
source=sales-quack
```

The same statement validator that `duckdb-sql` runs at config-load is
applied here at every invocation — so destructive verbs are rejected
before they reach the database. That's defense in depth, not a SQL
sandbox; the real boundary remains the Quack server's authorization
callback.

```bash
# Happy path
curl -s -X POST http://localhost:5555/api/tool/dev_duckdb_execute_sql/invoke \
    -d '{"sql": "SELECT count(*) AS n FROM remote.sales"}' \
    | jq '.result | fromjson'

# Destructive verbs come back as an AgentError envelope:
#   {"result": "{\"error\":\"statement rejected by policy: ...\"}"}
curl -s -X POST http://localhost:5555/api/tool/dev_duckdb_execute_sql/invoke \
    -d '{"sql": "DROP TABLE remote.sales"}' \
    | jq '.result | fromjson'
```

For production deployments, remove the `dev_duckdb_execute_sql` entry
from `tools.yaml` entirely (or flip `enabled: true` to anything else;
the server will refuse to start). The other toolsets
(`analytics_readonly`, `analytics_metadata`) are unaffected.

## Observability (OpenTelemetry)

The Compose stack includes an [OpenTelemetry
Collector][otelcol] receiving OTLP from Toolbox on port 4318 (HTTP),
plus a [Jaeger][jaeger] all-in-one instance for the visualization
side. The collector fans traces out to both the `debug` exporter
(stdout) and Jaeger via OTLP.

[otelcol]: https://github.com/open-telemetry/opentelemetry-collector
[jaeger]: https://www.jaegertracing.io/

Toolbox emits:

- A request-level span (`toolbox/server/tool/invoke`) per MCP tool
  invocation, from upstream's own instrumentation.
- A child `duckdb.query` span per SQL roundtrip (scope
  `github.com/googleapis/mcp-toolbox/internal/sources/duckdbquack`),
  with `db.system`, `toolbox.source.name`,
  `db.statement.parameter_count`, `db.response.rows`,
  `db.response.truncated`, `error.type`, and a `reattach` span event
  on the recovery path.
- Five DuckDB-scoped metrics: `duckdb.query.duration` (histogram, s),
  `duckdb.query.rows_returned` (histogram), `duckdb.query.errors_total`
  (counter, by `error.type`), `duckdb.query.truncated_total` (counter),
  `duckdb.connection.reattach_total` (counter).

The collector's `debug` exporter prints everything to stdout, so the
observability view is just:

```bash
# Tail spans + metrics in real time
docker compose logs -f otel-collector

# Look at the most recent `duckdb.query` span
docker compose logs otel-collector | grep -A20 'Name *: duckdb.query' | head -25
```

The Go OTel SDK's default metric reader flushes once per minute, so
metric data points show up in collector logs ~60 s after the first
invocation that produced them. Spans flush sooner (5 s default batch).

To send to a real backend instead of stdout, edit
[`otel-collector/config.yaml`](otel-collector/config.yaml) and replace
the `debug` exporter with `otlphttp`, `otlp`, `tempo`, etc. Toolbox
itself does not need to change — it talks OTLP to the collector, and
the collector translates onward.

The exporter configuration on the Toolbox side is two pieces:

- `--telemetry-otlp otel-collector:4318` — a **host:port**, not a URL.
  Toolbox prepends the scheme itself; passing `http://otel-collector:4318`
  here yields a malformed `https://http://otel-collector:4318/v1/metrics`.
- `OTEL_EXPORTER_OTLP_INSECURE=true` and
  `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` env vars — needed because
  the in-cluster collector is plaintext-HTTP and Toolbox's SDK defaults
  to gRPC.

### Distributed tracing across services (visualize in Jaeger)

The collector forwards every received span to Jaeger. Open
**http://localhost:16686**, pick a service from the dropdown
(`duckdb-quack-demo` for Toolbox-side spans, `trace-client` for the
demo client below, `langgraph-demo` for the agent), and click any
trace to see the full hierarchy on a flame graph.

#### Demo client: `trace-client` (no API key needed)

A small OTel-instrumented Python client lives at
[`trace-client/`](trace-client/). It builds a `client.invoke` span,
calls the `revenue_by_customer` tool via Toolbox's MCP JSON-RPC
endpoint, and embeds its W3C `traceparent` in the MCP
`_meta.traceparent` field. Toolbox extracts that and every
downstream span (the HTTP receiver, the tool dispatcher, and our
`duckdb.query`) joins the same trace.

```bash
# Bring up the OTel-instrumented stack
docker compose up -d quack-server toolbox otel-collector jaeger

# Run the demo client (profile-gated so `docker compose up` skips it)
docker compose --profile trace run --rm trace-client

# The client prints its trace_id at the end — paste it into the
# Jaeger UI's "Lookup by Trace ID" box.
```

Expected hierarchy (5 spans, 2 services):

```
trace-client       client.invoke
trace-client         POST (HTTP, auto-instrumented)
duckdb-quack-demo      toolbox/server/mcp/http
duckdb-quack-demo        tools/call revenue_by_customer
duckdb-quack-demo          duckdb.query   ← our span, with db.system,
                                            db.response.rows, etc.
```

#### Caveat: trace context flows via MCP `_meta`, not HTTP headers

Toolbox extracts incoming `traceparent` from the **MCP JSON-RPC
`_meta.traceparent` field** (see [`internal/server/mcp.go`][mcphandler]
in the fork), **not** from the HTTP `traceparent` header. Two
implications:

1. Hitting `/api/tool/<name>/invoke` (the REST convenience endpoint)
   never propagates trace context, regardless of what headers the
   client sets. The toolbox-side spans show up under a fresh trace ID.
2. MCP clients must put `traceparent` in `_meta.traceparent` — the
   typical OTel auto-instrumentation that just adds the HTTP header
   is not sufficient.

The `trace-client` script does this explicitly. The `langgraph` demo
relies on `toolbox-langchain` to propagate context; whether it does
depends on the SDK version (the MCP 2025-06-18 spec made the `_meta`
field standard, so a modern compliant SDK should). If your
`langgraph-demo` traces show up under a separate trace from
`duckdb-quack-demo`, that's the SDK not the wiring.

A writeup for an upstream issue requesting either HTTP-header
extraction on `/mcp` or richer client-SDK propagation lives in
[`NOTES.md`](NOTES.md).

[mcphandler]: https://github.com/mitja/mcp-toolbox-duckdb/blob/feat/duckdb-quack/internal/server/mcp.go

## LangGraph agent demo

```bash
docker compose --profile agent run --rm langgraph
```

The agent loads the `analytics_readonly` toolset over HTTP, then asks Claude
to summarize revenue for customers matching "gmbh". It prints the
intermediate tool calls and the final answer.

With OTel exporter env vars in place (the Compose file sets them by
default), the LangGraph process also emits an `agent.invoke` span and
auto-instrumented spans around every outgoing HTTP call (Toolbox, the
Anthropic API). They show up under service `langgraph-demo` in Jaeger.
Whether they join the same trace as `duckdb-quack-demo` depends on
whether `toolbox-langchain` propagates `_meta.traceparent` (see the
"Distributed tracing" caveat above).

## Wiring Claude Code

Copy [`claude-code/claude_config.example.json`](claude-code/claude_config.example.json)
into your Claude Code MCP config (typically `~/.claude.json` or
`./.mcp.json`). With the Compose stack running on `localhost:5555`, Claude
Code will list `revenue_by_customer` and `top_products` as callable tools.

## What's enforced where

Defense in depth, listed by layer (closest to the agent first):

1. **`duckdb-sql` tool, config-load validator** — multi-statement
   rejection, leading-keyword allowlist, forbidden-substring scan. Catches
   developer mistakes in `tools.yaml` (e.g., a stray `DROP TABLE`); refuses
   to start the server if any tool fails the policy.
2. **Tool invocation timeouts and row caps** — `policy.timeout` and
   `policy.max_rows` from the source config. Excess rows are dropped and
   the response sets `truncated: true`.
3. **Quack server authorization callback** — the `read_only` macro on
   the Quack server is the real security boundary. Even if a destructive
   statement somehow reaches the server (raw query, bypassed validator,
   bug), the server refuses anything that does not start with
   `SELECT|WITH|EXPLAIN|DESCRIBE|SHOW`.

This demo deliberately uses **default Quack authentication** (client TOKEN
must equal the bootstrap token). Production deployments should run the
Quack server behind a TLS-terminating reverse proxy and replace the default
authentication with a token-table macro.

## Troubleshooting

- **`toolbox` exits with `ATTACH ... Authorization failed`**: the
  `QUACK_TOKEN` in `.env` is not the same value the Quack server was
  bootstrapped with, OR you have edited `init.sql.tmpl` to activate the
  `quack_authorization_function` before clients have ATTACHed (the macro
  is also called for ATTACH's internal catalog queries). For the demo,
  defer the activation: keep `init.sql.tmpl` as-shipped and run the
  `SET GLOBAL` only after Toolbox has finished starting (see "Enabling
  server-side authz" below).
- **`localhost:5000` returns `AirTunes/...` or "empty reply"**: macOS
  binds 5000 to AirPlay by default. The demo publishes on host port
  `5555` to dodge it; use `http://localhost:5555`.
- **`tail -f /dev/null | duckdb` exits immediately**: the DuckDB CLI in the
  image does not support `quack`. Confirm the `DUCKDB_VERSION` build arg in
  `quack-server/Dockerfile` matches a release where Quack is bundled in
  `core_nightly` (currently v1.5.2+).
- **LangGraph container fails on `import toolbox_langchain`**: the demo
  pins `toolbox-langchain>=0.4.0`. If your local PyPI mirror is older,
  override with `pip install --upgrade toolbox-langchain` in the
  Dockerfile or pin a specific version.

### Enabling server-side authz (optional)

Layer 3 of the defense-in-depth model (`quack_authorization_function`
= `read_only`) is created but NOT activated by `init.sql.tmpl` because
Quack invokes the macro on the catalog probe queries that `ATTACH`
itself issues — activating it before the client ATTACH would break the
client's startup. To exercise server-side rejection of destructive
statements once Toolbox is up:

```bash
# After `docker compose up` reports "Server ready to serve!":
docker exec duckdb-quack duckdb /data/analytics.duckdb -cmd \
  "SET GLOBAL quack_authorization_function = 'read_only'" \
  -cmd ".quit"
```

A subsequent `INSERT`/`UPDATE`/`DELETE` reaching the server will be
rejected. The Toolbox-side tool-layer validator (Layer 1) already
rejects such statements at config load, so this layer matters only as
a backstop against bugs or future raw-SQL tool surfaces.

## Layout

```
.
├── docker-compose.yaml         # 3-service stack
├── tools.yaml                  # MCP Toolbox source + tool config
├── quack-server/
│   ├── Dockerfile              # Debian + DuckDB CLI + Quack
│   ├── entrypoint.sh           # envsubst init.sql.tmpl, then duckdb
│   ├── init.sql.tmpl           # INSTALL/LOAD quack, seed, authz, serve
│   └── seed.sql                # ~30 rows across sales + orders
├── langgraph/
│   ├── Dockerfile              # python:3.12-slim
│   ├── pyproject.toml          # toolbox-langchain + langgraph + langchain-anthropic
│   └── app.py                  # ReAct agent that loads analytics_readonly
├── claude-code/
│   └── claude_config.example.json
├── .env.example
└── README.md
```

## License

Apache 2.0 (matches the upstream MCP Toolbox project).
