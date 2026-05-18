# mcp-toolbox-duckdb-demo

End-to-end demo stack for the **MCP Toolbox DuckDB / Quack adapter** that
lives in the sibling fork [`mitja/mcp-toolbox-duckdb`](https://github.com/mitja/mcp-toolbox-duckdb)
(branch `feat/duckdb-quack`).

> [!NOTE]
> This repository and the DuckDB / Quack adapter in the sibling fork were developed with [Claude Code](https://claude.com/claude-code) (Anthropic's agentic CLI, Opus 4.7 at `xhigh` reasoning) doing the bulk of the iterative work — design discussions, the Go source/tool packages, integration tests, the Compose stack, the observability + load-test scaffolding, the docs, and this diagram — under human review and direction. The upstream contribution path (`googleapis/mcp-toolbox`) is human-driven from this fork as a base.

![Architecture of the demo stack](docs/stack.svg)

What's running, top to bottom:

- **Host clients** — Claude Code via MCP at `localhost:5555/mcp`,
  `curl`/notebook against `/api/tool/<name>/invoke`, and your browser
  for five web UIs: **MCP Inspector** (`:6274`), the built-in
  **Toolbox UI** (`:5555/ui` — tool browser + invoker shipped with
  Toolbox), **Cube Playground** (`:4000` — the semantic-layer
  authoring + query UI), **Apache Superset** (`:8088` — the BI
  surface, with a pre-built dashboard over Cube), and **Jaeger**
  (`:16686`).
- **In-network clients (profile-gated)** — `trace-client`
  (`--profile trace`, single-call OTel demo), `trace-load`
  (`--profile load`, concurrent multi-source burst with PASS/FAIL
  verifier), `langgraph` (`--profile agent`, needs Anthropic API
  key), `inspector` (`--profile inspect`, MCP Inspector UI + proxy).
- **Toolbox** (`toolbox:5000`) — the MCP Toolbox server from the
  fork. One in-process DuckDB (CGO `duckdb-go`) per source, three
  sources total: `sales-quack` and `inventory-quack` are
  single-`ATTACH`, and `combined-analytics` is multi-attach (both
  Quack servers wired into the same DuckDB so a tool can join across
  catalogs in one statement). `duckdb-sql` tools can also set
  `push_down_to_remote: true` to ship the whole statement through
  `quack_query()` so a join executes on the remote side. The same
  statement validator runs at config-load and per-invocation; OTel
  span `duckdb.query` plus 5 metrics emit on every call.
- **`quack-server` + `quack-server-2` (`:9494`)** — two separate
  DuckDB+Quack processes wired as the `sales-quack` and
  `inventory-quack` sources (and jointly as `combined-analytics`).
  Both run the same image; `quack-server-2` additionally mounts
  `product_price_history.parquet` and exposes it as a view in
  `main` via `read_parquet(...)` so the Toolbox-side ATTACH sees it
  alongside the regular `products` table. Both apply the
  `read_only` authorization callback — the real security boundary;
  any destructive statement that somehow gets past Toolbox is
  refused here.
- **`otel-collector` + `jaeger`** — always-on, receive spans/metrics
  from anything OTel-instrumented (Toolbox plus any active client).
- **`cube` + `cube-seed`** — Cube Core (Apache 2.0) running over a
  small DuckDB seeded from the same `seed.sql` the primary
  quack-server uses. Two cubes (`sales`, `orders`) in
  [`cube/model/cubes/`](cube/model/cubes/) define measures +
  dimensions. The REST/SQL APIs serve BI tools; the same cubes are
  the authoring authority for the `cube_*` Toolbox tools in the
  `analytics_cube_backed` toolset. See "Semantic-layer integration
  with Cube" below for the architecture story.
- **`superset`** — Apache Superset (Apache 2.0) wired to Cube via
  the Postgres SQL API. An idempotent bootstrap creates an admin
  user, registers Cube as a database, and pre-builds one dataset +
  one chart + one dashboard against the `sales` cube. The dashboard
  is the visual proof that the semantic model serves a real BI
  client. See "BI dashboard with Apache Superset" below.

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

## Browse tools with MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector)
is a web UI for poking at any MCP server: list tools/resources/prompts,
inspect each one's schema, fill in parameters, and run a tool call —
all without an LLM or SDK in the loop. The Compose file ships it as
a profile-gated service so it does not start by default.

```bash
docker compose --profile inspect up -d inspector
```

Then open:

```
http://localhost:6274/?transport=streamable-http&serverUrl=http%3A%2F%2Ftoolbox%3A5000%2Fmcp
```

The query params pre-fill the connection form with **Transport Type
= Streamable HTTP** and **URL = `http://toolbox:5000/mcp`**. Click
**Connect**, then **List Tools** in the left pane. You should see
all fourteen tools — `revenue_by_customer`, `top_products`,
`low_stock_items`, `inventory_summary`, `price_history_for_product`,
`current_prices`, `product_orders_overview`, `list_catalogs`,
`list_remote_schemas`, `list_remote_tables`, `describe_sales`,
`describe_orders`, `summarize_sales`, and the dev-only
`dev_duckdb_execute_sql` — with their input schemas and
descriptions. Pick one, fill in params, and click **Run Tool** to
see the JSON response shape live.

A few notes:

- The URL is `http://toolbox:5000/mcp` (the in-network hostname),
  **not** `http://localhost:5555/mcp`. The Inspector's web UI sends
  the URL to a proxy process inside the inspector container, which
  is the side that makes the connection — `localhost` from there
  resolves to the inspector itself, not Toolbox.
- The Compose service sets `DANGEROUSLY_OMIT_AUTH=true` so the UI
  is reachable without a session token. Fine for a localhost demo;
  drop the env var (and grab the auto-generated token from
  `docker compose logs inspector`) before binding ports 6274/6277
  to anything but loopback.
- The Inspector's `tools/call` requests go through Toolbox just
  like an SDK or `curl`, so they hit the same policy validator,
  row caps, and Quack authorization callback you see exercised
  elsewhere in this demo.

## Metadata tools

The demo `tools.yaml` exposes five toolsets across three sources
(`sales-quack`, `inventory-quack`, and the multi-attach
`combined-analytics`):

- **`analytics_readonly`** — `revenue_by_customer`, `top_products`. The
  curated, parameterized queries an agent uses to answer questions
  about the sales data.
- **`analytics_metadata`** — `list_catalogs`, `list_remote_schemas`,
  `list_remote_tables`, `describe_sales`, `describe_orders`,
  `summarize_sales`. The discovery tools an agent uses to learn the
  catalog before constructing a query. All six are parameterless from
  the agent's perspective — schema/table scope is baked into
  `tools.yaml` so deployment-time RBAC, not runtime tool calls,
  controls what the agent can see.
- **`inventory_readonly`** — `low_stock_items`, `inventory_summary`,
  `price_history_for_product`, `current_prices`. The last two are
  backed by a parquet file mounted next to the remote DuckDB on
  `quack-server-2` — see [Parquet behind a remote Quack](#parquet-behind-a-remote-quack).
  Curated read-only tools against the second Quack server.
- **`cross_catalog`** — `product_orders_overview`. The single
  cross-catalog tool that joins inventory products with sales
  orders inside one in-process DuckDB. See [Cross-catalog queries
  and pushdown](#cross-catalog-queries-and-pushdown) for why this
  one is special.
- **`analytics_dev`** — `dev_duckdb_execute_sql`, the agent-supplied
  ad-hoc SQL tool. Gated behind `enabled: true` and not loaded
  unless explicitly attached. See
  [Development-only ad-hoc SQL](#development-only-ad-hoc-sql-analytics_dev)
  below.

Smoke-test the metadata tools through the HTTP API:

```bash
# List the toolset's contents
curl -s http://localhost:5555/api/toolset/analytics_metadata | jq '{tools: (.tools | keys)}'

# Discovery flow: catalogs -> schemas -> tables -> describe a table.
# Note: the `Content-Type: application/json` header is required — curl
# defaults to `application/x-www-form-urlencoded` for -d, which Toolbox
# accepts with HTTP 200 but an empty body (no JSON to parse, no tool
# invocation actually runs).
H='Content-Type: application/json'
curl -s -X POST -H "$H" http://localhost:5555/api/tool/list_catalogs/invoke      -d '{}' | jq '.result | fromjson'
curl -s -X POST -H "$H" http://localhost:5555/api/tool/list_remote_tables/invoke -d '{}' | jq '.result | fromjson'
curl -s -X POST -H "$H" http://localhost:5555/api/tool/describe_sales/invoke     -d '{}' | jq '.result | fromjson'

# Per-column statistics
curl -s -X POST -H "$H" http://localhost:5555/api/tool/summarize_sales/invoke    -d '{}' | jq '.result | fromjson'

# with mlr (or --omd or --ocsv)
curl -sS -X POST -H "$H" \
      http://localhost:5555/api/tool/describe_sales/invoke -d '{}' \
    | jq -r '.result | fromjson | .rows' \
    | mlr --ijson --opprint cat
```

The metadata tools that target the remote DuckDB (everything except
`list_catalogs`) push their SQL through Quack's `quack_query()` table
function. The Toolbox-side `information_schema` view of an ATTACHed
catalog is intentionally incomplete (DuckDB does not push catalog
enumeration through ATTACH), so `quack_query()` is the route that
sees the live remote schema.

## Development-only ad-hoc SQL (`analytics_dev`)

The `analytics_dev` toolset contains a single tool:
`dev_duckdb_execute_sql`. This is a **dev-only** surface —
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
curl -s -X POST -H 'Content-Type: application/json' http://localhost:5555/api/tool/dev_duckdb_execute_sql/invoke \
    -d '{"sql": "SELECT count(*) AS n FROM remote.sales"}' \
    | jq '.result | fromjson'

# Destructive verbs come back as an AgentError envelope:
#   {"result": "{\"error\":\"statement rejected by policy: ...\"}"}
curl -s -X POST -H 'Content-Type: application/json' http://localhost:5555/api/tool/dev_duckdb_execute_sql/invoke \
    -d '{"sql": "DROP TABLE remote.sales"}' \
    | jq '.result | fromjson'
```

For production deployments, remove the `dev_duckdb_execute_sql` entry
from `tools.yaml` entirely (or flip `enabled: true` to anything else;
the server will refuse to start). The other toolsets
(`analytics_readonly`, `analytics_metadata`, `inventory_readonly`,
`cross_catalog`) are unaffected.

## Rendering JSON output as tables

The spec §7 envelope is great for programs and a bit dense to
eyeball — especially `summarize_sales` with its 12 output columns.
A few CLI tools turn the `.rows` array into something pasteable.

### Miller (`mlr`) — most flexible

`brew install miller`. Reads JSON, writes to terminal / markdown /
CSV / TSV / much more, all switched by a single flag.

```bash
H='Content-Type: application/json'

# 1. Pretty terminal table
curl -sS -X POST -H "$H" http://localhost:5555/api/tool/describe_sales/invoke -d '{}' \
  | jq '.result | fromjson | .rows' \
  | mlr --ijson --opprint cat
# column_name data_type     is_nullable
# id          INTEGER       NO
# customer    VARCHAR       NO
# amount      DECIMAL(18,2) NO
# order_date  DATE          NO

# 2. Markdown (paste into a PR description or notebook)
curl -sS -X POST -H "$H" http://localhost:5555/api/tool/describe_sales/invoke -d '{}' \
  | jq '.result | fromjson | .rows' \
  | mlr --ijson --omd cat
# | column_name | data_type | is_nullable |
# | --- | --- | --- |
# | id | INTEGER | NO |
# | customer | VARCHAR | NO |
# | amount | DECIMAL(18,2) | NO |
# | order_date | DATE | NO |

# 3. CSV (pipe to a spreadsheet, or share as a file)
curl -sS -X POST -H "$H" http://localhost:5555/api/tool/describe_sales/invoke -d '{}' \
  | jq '.result | fromjson | .rows' \
  | mlr --ijson --ocsv cat
```

For `summarize_sales` (12 columns), `--opprint --barred` keeps the
columns aligned and visually grouped.

### `jq` + `column -t` — zero install

Lives on every dev box. Slightly more verbose because you list the
columns yourself, but works without installing anything.

```bash
curl -sS -X POST -H 'Content-Type: application/json' \
    http://localhost:5555/api/tool/describe_sales/invoke -d '{}' \
  | jq -r '.result | fromjson |
      (["column_name","data_type","is_nullable"] | @tsv),
      (.rows[] | [.column_name, .data_type, .is_nullable] | @tsv)' \
  | column -t -s$'\t'
# column_name  data_type      is_nullable
# id           INTEGER        NO
# customer     VARCHAR        NO
# amount       DECIMAL(18,2)  NO
# order_date   DATE           NO
```

### Honorable mentions

- **`jtbl`** (`pip install jtbl`) — one-line `... | jtbl` for an
  ASCII terminal table. Less flexible than Miller (no markdown / CSV
  output), but very low ceremony.
- **VisiData** (`vd`) — interactive TUI spreadsheet, reads JSON
  natively (`vd path/to.json`, or `... | vd -f json`). Overkill for
  small results; unbeatable for browsing a 5000-row truncated SELECT.

### Which to pick

| Goal | Pick |
|------|------|
| Quick eyeball in a terminal | `mlr --opprint` (or `jtbl` if you have it) |
| Paste into a PR / notebook | `mlr --omd` |
| Share as a file or pipe to a sheet | `mlr --ocsv` |
| No installs allowed | `jq -r '... \| @tsv' \| column -t -s$'\t'` |
| Browse a big result interactively | VisiData |

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
2. MCP clients must put `traceparent` in `_meta.traceparent`. The
   typical OTel auto-instrumentation that just adds the HTTP
   `traceparent` header is not sufficient on its own.

`toolbox-langchain >= 1.0` (and `toolbox-core` underneath) injects
`_meta.traceparent` automatically when constructed with
`telemetry_enabled=True` — the `langgraph` demo does this, and the
agent's `agent.invoke` span joins the same trace as the
toolbox-side `duckdb.query` span (see the [LangGraph
demo](#langgraph-agent-demo) for the expected hierarchy). The
`trace-client` script demonstrates the same wiring without an LLM
dependency.

A writeup for an upstream issue requesting HTTP-header extraction on
`/mcp` (as a defense-in-depth that also covers REST callers and
older MCP SDKs) lives in [`NOTES.md`](NOTES.md).

[mcphandler]: https://github.com/mitja/mcp-toolbox-duckdb/blob/feat/duckdb-quack/internal/server/mcp.go

## Concurrent multi-source load test

[`trace-client/load_test.py`](trace-client/load_test.py) fires N
parallel MCP `tools/call` requests fanned out across **six tools
spanning all three sources** — two against `sales-quack`
(`revenue_by_customer`, `top_products`), three against
`inventory-quack` (`low_stock_items`, `inventory_summary`, and
`price_history_for_product`, the last backed by a parquet file
mounted next to the remote DuckDB), and one against
`combined-analytics` (`product_orders_overview`, the cross-catalog
JOIN that runs locally after rows stream from both Quack servers).
Each request generates its own root span, embeds W3C `traceparent`
in MCP `_meta`, and is fully independent. After the burst, the
script polls the Jaeger HTTP API and asserts:

1. Every request returned valid (non-error) JSON.
2. Every emitted trace ID resolves to a trace that contains a
   `duckdb.query` span (no dropped exports).
3. Each trace's `duckdb.query` is a descendant of the corresponding
   `client.invoke*` root span (no broken stitches under concurrency).

The script exits non-zero on any failure, so it doubles as a smoke
test you can drop into CI.

```bash
docker compose --profile load run --rm trace-load                 # N=20 (default)
N_CONCURRENT=50 docker compose --profile load run --rm trace-load  # bump it
```

Sample output at N=25:

```
firing 25 concurrent calls across 6 tools (3 sources: sales-quack, inventory-quack [incl. parquet-backed view], combined-analytics)...

requests:   25 sent, 25 ok, 0 error
wall time:  0.07s
throughput: 366.0 req/s
latency:    avg 0.040s  p50 0.040s  p95 0.056s  max 0.059s
by tool:
  inventory_summary          4 sent,   4 ok, row_count(s)=[7]
  low_stock_items            4 sent,   4 ok, row_count(s)=[7]
  price_history_for_product  4 sent,   4 ok, row_count(s)=[5]
  product_orders_overview    4 sent,   4 ok, row_count(s)=[20]
  revenue_by_customer        5 sent,   5 ok, row_count(s)=[1]
  top_products               4 sent,   4 ok, row_count(s)=[3]

Jaeger lookup:
  complete traces:    25/25    (contain duckdb.query)
  spans stitched:     25/25    (duckdb.query under client.invoke*)

result: PASS
```

`product_orders_overview` returns 20 rows because the inventory seed
has 20 products and the tool LEFT JOINs each one with sales orders;
`price_history_for_product` returns five rows because each product
has four historical price entries plus one current row in the
parquet fixture; the other tools return at most a handful of rows
because they aggregate. The local-execution path (the cross-catalog
tool) shows up alongside the pushdown path (single-source tools
plus the parquet-backed `price_history_for_product`) in the same
Jaeger trace list — easy to compare `duckdb.query` durations
side-by-side.

A few notes:

- Toolbox-side spans land a few seconds after the client spans (each
  side has its own OTel batch flush + the collector batches in
  between), so the verifier keeps re-fetching each trace until it
  sees the `duckdb.query` span — up to a 60s budget.
- `trace-load` reuses the `trace-client` image (overrides the
  entrypoint) but does its own concurrent fan-out — the script lives
  alongside the single-call demo for easy diffing.

## Cross-catalog queries and pushdown

The default tool surface (`revenue_by_customer`, `top_products`,
`low_stock_items`, `inventory_summary`) wires each tool to **one**
Quack source — DuckDB pushes filters and projections down to that
remote, joins / aggregates / sorts happen in the in-process DuckDB
after rows stream back. For a single-source `SELECT … GROUP BY …
LIMIT` over a typical table that's small working memory regardless
of how large the remote dataset is.

When you actually need to combine data from two Quack servers in
one query, the demo also ships a third source — `combined-analytics`
— that ATTACHes both servers into the same in-process DuckDB via
the adapter's `additional_attachments` config. The
`product_orders_overview` tool joins `inventory_remote.products`
with `sales_remote.orders` in a single SQL:

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  http://localhost:5555/api/tool/product_orders_overview/invoke -d '{}' \
  | jq '.result | fromjson | .rows[:3]'
```

```json
[
  {"category":"Hardware","name":"Sprocket","stock_qty":18,"reorder_at":30,"units_ordered":49,"distinct_customers":4},
  {"category":"Hardware","name":"Widget",  "stock_qty":120,"reorder_at":25,"units_ordered":48,"distinct_customers":5},
  {"category":"Hardware","name":"Gizmo",   "stock_qty":72, "reorder_at":20,"units_ordered":16,"distinct_customers":5}
]
```

The join is executed **locally** by the in-process DuckDB after
rows stream from each remote — that's the architectural cost of a
cross-catalog query.

### Tips for keeping work on the remote

Most curated tools should look like the single-source ones — the
remote is sized for the dataset, the Toolbox process isn't. Some
heuristics that keep the local DuckDB instance lightweight:

- **One source per tool when you can.** Each duckdb-quack source
  pushes filters and projections through ATTACH; once you reference
  two attached aliases in one query, DuckDB has to pull rows back
  from each side to join them locally. Wire two single-source tools
  and let the agent do the second hop, unless the query semantics
  truly require a join in the database.
- **Project narrowly.** `SELECT customer, SUM(amount)` is cheaper
  than `SELECT *`; the Quack scanner pushes the column list, so
  unselected columns never cross the wire. Avoid `SELECT *` in
  tool config even when the table is small.
- **Filter early.** A `WHERE` clause on the attached table
  (`WHERE customer ILIKE ?` etc.) pushes down — the remote scans
  fewer rows and ships less data. A `WHERE` on the *result* of a
  local aggregate (`HAVING SUM(amount) > ?`) doesn't push and runs
  locally.
- **Aggregate where the data lives.** Single-source aggregates
  (`GROUP BY customer FROM remote.sales`) typically stream — the
  local hash table is sized by the number of distinct groups, not
  by the source row count. Cross-source aggregates (group by a key
  that joins both sides) materialize both inputs locally; prefer
  doing the join inside one remote when you can.
- **Always set a `LIMIT` for "top-N" tools.** With a `LIMIT N
  ORDER BY x`, DuckDB streams + early-terminates. Without it, an
  `ORDER BY` has to materialize the entire result set locally
  before sorting.
- **For metadata, prefer the dedicated tools.** `list_remote_tables`,
  `describe_*`, and `summarize_*` go through Quack's `quack_query()`
  table function — full SQL passthrough to the remote, no local
  materialization. Pulling the same data via `SELECT * FROM
  information_schema.tables` over an ATTACH would be slower and
  fatter.
- **`max_rows` is the local-memory safety net, not an architectural
  decision.** Hitting it means a slow query has already pulled most
  of its rows across the wire before being truncated. The real
  guardrails are an explicit `LIMIT` in the tool's SQL and a tight
  `WHERE` clause; `max_rows` just keeps a runaway query from
  blowing up Toolbox's RAM.

The single-source tools in this demo follow all of the above; the
multi-source `product_orders_overview` is the deliberate exception
so you can compare query plans side-by-side in the Jaeger UI.

### Parquet behind a remote Quack

The remote DuckDB inside a Quack server is *just DuckDB* — anything it
can read, it can expose as a view in `main`, and the Toolbox-side
in-process DuckDB sees it through the existing `ATTACH` like any
other table. The adapter itself doesn't need to know.

`quack-server-2` ships with `product_price_history.parquet` mounted
at `/data/`. The inventory seed wraps it in a view:

```sql
CREATE OR REPLACE VIEW main.product_price_history AS
    SELECT * FROM read_parquet('/data/product_price_history.parquet');
```

Two tools on the `inventory-quack` source exercise it:

```bash
# Single streaming scan of the parquet view, filtered by ILIKE.
curl -s -X POST -H 'Content-Type: application/json' \
  http://localhost:5555/api/tool/price_history_for_product/invoke \
  -d '{"product_pattern":"Widget"}' \
  | jq '.result | fromjson | .rows[:3]'

# Join the products table with the parquet view's current rows.
curl -s -X POST -H 'Content-Type: application/json' \
  http://localhost:5555/api/tool/current_prices/invoke -d '{}' \
  | jq '.result | fromjson | .rows[:3]'
```

```json
[
  {"category":"Bearings","product_name":"Bearing 608ZZ","current_price":"3.25","price_since":"2026-01-18T00:00:00Z","last_change_reason":"audit-correction","stock_qty":140},
  {"category":"Bearings","product_name":"Bearing 6203","current_price":"5.40","price_since":"2025-05-16T00:00:00Z","last_change_reason":"supplier-change","stock_qty":28},
  {"category":"Cabling","product_name":"Cable Loom 2m","current_price":"6.80","price_since":"2025-12-09T00:00:00Z","last_change_reason":"seasonal-adjust","stock_qty":95}
]
```

**Why the join uses `push_down_to_remote: true`.** The Toolbox-side
DuckDB can reject a plain `SELECT … FROM inventory_remote.products p
LEFT JOIN inventory_remote.product_price_history h …` with
*"Multiple streaming scans … not currently supported"* — when its
planner picks a streaming plan for both ATTACHed-table references,
DuckDB cannot drive more than one streaming-scan source per
pipeline. The `current_prices` tool sets `push_down_to_remote:
true`, which the duckdb-quack adapter implements by routing the
whole statement through `quack_query(<source-uri>, '<sql>', disable_ssl := …)`:
the SQL ships to the remote, executes next to the parquet file,
and only the result rows stream back as one scan. The flag works
for any single-source `duckdb-quack` tool without bound parameters
— template parameters still substitute before the wrap. For
cross-source joins (where there is no single remote to push to)
the manual `quack_query('<primary-uri>', '<sql>', …)` wrapper is
still the route. See [`tools.yaml`](tools.yaml) for the
`current_prices` statement and [`NOTES.md`](NOTES.md) for the
adapter-side implementation notes (and the upstream DuckDB
limitation the flag works around).

The fixture itself is reproducible — re-run the generator if you
change the schema:

```bash
uv run --no-project --with duckdb python3 quack-server/gen_price_history.py
```

## Semantic-layer integration with Cube

In a real deployment, BI tools (Superset, Tableau, …) and agents
typically grow **two separate semantic layers** over the same
warehouse — duplicated metric definitions, duplicated joins,
duplicated drift. This demo wires [Cube Core](https://cube.dev)
(Apache 2.0) as a sidecar and shows the **one-model-two-surfaces**
pattern: a single set of cubes drives both Cube's BI APIs *and* a
corresponding family of MCP Toolbox tools.

Cube runs on `localhost:4000` (REST + GraphQL + Cube Playground) and
`localhost:15432` (Postgres-protocol SQL API). The cube definitions
live in [`cube/model/cubes/`](cube/model/cubes/) — two cubes,
`sales` and `orders`, with measures (`revenue`, `total_qty`,
`distinct_customers`, …), dimensions (`customer`, `product`,
`order_date`), and a join. Open `http://localhost:4000` to land in
the Cube Playground and explore them visually.

**The same measures back two tools on the Toolbox side.** They live
in the `analytics_cube_backed` toolset and return rows whose column
names use Cube's `<cube>.<member>` shape — so the agent's response
shape is the same shape a BI client would see:

```bash
# BI surface (what Superset/Tableau would hit):
curl -s 'http://localhost:4000/cubejs-api/v1/load' \
  --data-urlencode 'query={"measures":["sales.revenue","sales.order_count"],"dimensions":["sales.customer"],"order":[["sales.revenue","desc"]]}' \
  | jq '.data[:3]'

# Agent surface (what Claude Code / LangGraph hit through MCP Toolbox):
curl -s -X POST http://localhost:5555/api/tool/cube_sales_revenue_by_customer/invoke \
  -H 'Content-Type: application/json' -d '{}' \
  | jq '.result | fromjson | .rows[:3]'
```

Both return the same rows from the same model:

```json
[
  {"sales.customer": "Daniel SARL", "sales.revenue": "3850.00", "sales.order_count": 2},
  {"sales.customer": "Alice GmbH",  "sales.revenue": "2661.65", "sales.order_count": 4},
  {"sales.customer": "Bob Corp",    "sales.revenue": "2620.20", "sales.order_count": 3}
]
```

The same pattern works for `cube_orders_top_products` (measures
`orders.total_qty` + `orders.distinct_customers`, dimension
`orders.product`).

For an interactive side-by-side comparison that runs both surfaces
and asserts the rows match, see
[`notebooks/walkthrough.ipynb`](notebooks/walkthrough.ipynb) §9
("Semantic-layer integration with Cube — both surfaces").

### Sync today: codegen; runtime federation: not yet

The `cube_*` tools in `tools.yaml` are **generated** from the cube
YAML by [`cube/gen_toolbox_from_cube.py`](cube/gen_toolbox_from_cube.py).
The script reads `cube/model/cubes/*.yml` (the model authority) plus
[`cube/codegen.yml`](cube/codegen.yml) (a small human-authored list
of "which slices to expose as named tools"), and emits the
corresponding `duckdb-sql` tool entries into the sentinel-managed
block between `# BEGIN cube-generated` and `# END cube-generated`
in `tools.yaml`. To add or change a Cube-backed tool:

```bash
# 1. Edit cube YAML (measure definitions, sql fragments, joins, ...)
$EDITOR cube/model/cubes/sales.yml

# 2. Edit codegen.yml if adding a new slice (new tool_name × measures × dimensions)
$EDITOR cube/codegen.yml

# 3. Regenerate the tools.yaml block
uv run --no-project --with pyyaml python3 cube/gen_toolbox_from_cube.py
# (Use --stdout to preview the snippet without touching tools.yaml.)

# 4. If you added a slice: add its tool_name to the analytics_cube_backed
#    toolset in tools.yaml by hand (the toolset entry is not generated).

# 5. Restart Toolbox to pick up the changes
docker compose restart toolbox
```

What's *still* manual: the `analytics_cube_backed` toolset entry
(adding a generated tool's name to the toolset list), and tools with
bound `parameters:` — the codegen only emits parameterless aggregates
today.

**Runtime federation is what we'd actually want** — the in-process
DuckDB in Toolbox `ATTACH`ing Cube's SQL API directly, so the
Toolbox tool's SQL could just `SELECT * FROM cube_pg.sales` and let
Cube compile measures. We tried that with DuckDB's `postgres_scanner`
extension; it doesn't work today. The `ATTACH` succeeds, but
`postgres_scanner` issues catalog probes (`pg_indexes`, `pg_class`
compound joins) and wraps reads in `COPY … TO STDOUT (FORMAT
binary)` — neither of which Cube's pg-protocol implementation
supports. Until Cube widens its pg surface (or `postgres_scanner`
gains a "minimum-Postgres" mode), codegen is the tightest sync we
can offer. See [`NOTES.md`](NOTES.md) for the compatibility
writeup.

**Runtime federation is what we'd actually want** — the in-process
DuckDB in Toolbox `ATTACH`ing Cube's SQL API directly, so the
Toolbox tool's SQL could just `SELECT * FROM cube_pg.sales` and let
Cube compile measures. We tried that with DuckDB's `postgres_scanner`
extension; it doesn't work today. The `ATTACH` succeeds, but
`postgres_scanner` issues catalog probes (`pg_indexes`, `pg_class`
compound joins) and wraps reads in `COPY … TO STDOUT (FORMAT
binary)` — neither of which Cube's pg-protocol implementation
supports. Until Cube widens its pg surface (or `postgres_scanner`
gains a "minimum-Postgres" mode), the manual-sync pattern is the
honest demo. See [`NOTES.md`](NOTES.md) for the compatibility
writeup.

### Where Cube fits in the wider story

A few honest tradeoffs:

- **Pro:** One semantic model. BI dashboards and agents query the
  same measures by name; an audit of "what does `sales.revenue`
  mean" has one answer in `cube/model/cubes/sales.yml`.
- **Pro:** Cube already supports many warehouses (DuckDB, Postgres,
  BigQuery, Snowflake, …); swapping the storage backend doesn't
  change the agent contract.
- **Con (today):** Cube can grow new measures faster than the
  curated `analytics_cube_backed` toolset surfaces them. The
  codegen flow above closes the SQL-fragment gap (a measure SQL
  change re-flows into the tool); it does *not* invent new slices.
  A runtime ATTACH (blocked by Cube↔postgres_scanner compatibility
  today) would close both gaps.
- **Con (architectural):** Cube adds a service to the deployment.
  For very simple agent surfaces (a single curated SQL tool, no BI
  consumer), it's overkill — write `duckdb-sql` tools directly.

## BI dashboard with Apache Superset

Apache Superset (Apache 2.0) is wired in as a sidecar to demonstrate
the **other** half of the one-model-two-surfaces story: the same
cubes that back the `cube_*` Toolbox tools also drive a real BI
dashboard, served by Superset over Cube's Postgres SQL API. No
warehouse-side definitions are duplicated; both surfaces query the
same `sales` cube.

```bash
docker compose up -d cube superset
# First-boot bootstrap takes ~60s while Superset migrates its
# metastore. Watch `docker compose logs -f superset | grep bootstrap`.
open http://localhost:8088   # admin / admin
```

After the bootstrap finishes, three things exist in Superset:

| Object        | Name                                          | What it is                                                      |
|---------------|-----------------------------------------------|-----------------------------------------------------------------|
| Database      | `Cube (semantic layer)`                       | SQLAlchemy URL `postgresql+psycopg2://cube:cube@cube:15432/db`. |
| Dataset       | `sales`                                       | Maps to the `sales` cube; saved metrics `revenue` (= `MEASURE(revenue)`) and `order_count`. |
| Chart         | `Revenue by customer (Cube)`                  | Bar chart, dimension `customer`, metric `revenue`.              |
| Dashboard     | `MCP Toolbox demo — Cube semantic layer`      | Hosts the chart at `/superset/dashboard/mcp-toolbox-cube-demo/`.|

The dashboard URL: <http://localhost:8088/superset/dashboard/mcp-toolbox-cube-demo/>.

![Superset dashboard — Revenue by customer (Cube)](docs/superset_dashboard.png)

The same six rows you see in the bar chart are what
[`cube_sales_revenue_by_customer`](tools.yaml) returns through MCP
and what Cube's REST API returns directly — verified end-to-end by
the assertion in walkthrough notebook §9.

The embedded PNG is generated by
[`superset/screenshot.py`](superset/screenshot.py) (Playwright +
headless Chromium). Regenerate after dashboard changes:

```bash
uv run --no-project --with playwright python3 superset/screenshot.py
```

### How the bootstrap works

[`superset/bootstrap.sh`](superset/bootstrap.sh) and
[`superset/bootstrap.py`](superset/bootstrap.py) implement an
idempotent setup driven by Superset's REST API:

1. `superset db upgrade` (metastore migrations).
2. `superset fab create-admin` (admin / admin for the demo).
3. `superset init` (default roles + permissions).
4. Start `gunicorn`; wait for `/health`.
5. POST the Cube database connection if it doesn't already exist.
6. POST the dataset over the `sales` cube. Superset's
   auto-introspection picks up Cube's columns (measures appear as
   columns: `revenue`, `order_count`, `avg_order_value`; plus
   dimensions and `__user`/`__cubeJoinField` internals).
7. PUT saved metrics on the dataset: `revenue` →
   `MEASURE(revenue)`, `order_count` → `MEASURE(order_count)`.
   Without these, Superset would try to `SUM(revenue)` and Cube
   would refuse (the column is already a measure).
8. POST the chart and the dashboard, then link them on both sides
   (`position_json` on the dashboard *and* `dashboards: [id]` on
   the chart — Superset 4.x needs both).

Every step short-circuits if the object already exists, so
`docker compose restart superset` is a no-op. The metastore lives
in a Compose volume (`superset-data`) so dashboards you create
yourself in the UI also persist.

### A few honest tradeoffs

- **Superset speaks Postgres; Cube emulates Postgres.** This
  combination works (Cube ships explicit Superset compatibility),
  but it is a narrower surface than DuckDB's `postgres_scanner`
  needs — see [`NOTES.md`](NOTES.md) for why we couldn't use the
  same Postgres bridge from Toolbox.
- **The bootstrap creates one chart for one cube.** Add more
  by extending [`superset/bootstrap.py`](superset/bootstrap.py)
  (or interactively in the UI — the dashboard will persist).
- **Demo credentials are admin / admin.** Fine for localhost; do
  not deploy this layout to anything reachable beyond loopback
  without rotating `SUPERSET_SECRET_KEY` and the admin password.

## LangGraph agent demo

```bash
docker compose --profile agent run --rm langgraph
```

The agent loads the `analytics_readonly` toolset over HTTP, then asks Claude
to summarize revenue for customers matching "gmbh". It prints the
intermediate tool calls and the final answer.

With OTel exporter env vars in place (the Compose file sets them by
default), the LangGraph process emits an `agent.invoke` span and
auto-instrumented spans around every outgoing HTTP call (Toolbox,
the Anthropic API). They show up under service `langgraph-demo` in
Jaeger and stitch with the toolbox-side spans into a single trace:

```
langgraph-demo     agent.invoke
langgraph-demo       POST                              (Anthropic API)
langgraph-demo       tools/call revenue_by_customer    (MCP client span)
duckdb-quack-demo      toolbox/server/mcp/http
duckdb-quack-demo        tools/call revenue_by_customer
duckdb-quack-demo          duckdb.query                ← our Go span
```

The stitch relies on `ToolboxClient(..., protocol=Protocol.MCP_LATEST,
telemetry_enabled=True)` (see [`langgraph/app.py`](langgraph/app.py));
`telemetry_enabled=True` is what makes the SDK inject
`_meta.traceparent` into each MCP `tools/call`.

## Wiring Claude Code

Copy [`claude-code/claude_config.example.json`](claude-code/claude_config.example.json)
into your Claude Code MCP config (typically `~/.claude.json` or
`./.mcp.json`). With the Compose stack running on `localhost:5555`,
Claude Code will list all twelve tools (the default `/mcp` endpoint
exposes the full surface). To scope it to just one toolset, change
the URL in the config from `http://localhost:5555/mcp` to
`http://localhost:5555/mcp/<toolset_name>` — `analytics_readonly`,
`analytics_metadata`, `inventory_readonly`, or `cross_catalog`
(don't expose `analytics_dev` to a production agent).

## Wiring Pi (pi.dev)

[Pi](https://pi.dev) doesn't speak MCP natively, but several
community extensions add it — the most "official" one is
[`pi-mcp-extension`](https://pi.dev/packages/pi-mcp-extension)
in Pi's package registry. Other options include
[`scaryrawr/pi-mcp`](https://github.com/scaryrawr/pi-mcp) (reads
`.mcp.json`, supports stdio + Streamable HTTP) and
[`nicobailon/pi-mcp-adapter`](https://github.com/nicobailon/pi-mcp-adapter)
(token-efficient single-proxy-tool model — useful when an MCP
server exposes many tools and you don't want them all in the
system prompt).

Copy [`pi/pi_config.example.json`](pi/pi_config.example.json) into
your Pi config (project-level `.pi/mcp.json` or global
`~/.pi/agent/mcp.json`). The example registers four MCP servers,
each scoped to a different Toolbox toolset:

| Server entry                  | Toolbox route                    | Lifecycle |
|-------------------------------|----------------------------------|-----------|
| `toolbox-analytics-readonly`  | `/mcp/analytics_readonly`        | `eager`   |
| `toolbox-analytics-metadata`  | `/mcp/analytics_metadata`        | `eager`   |
| `toolbox-inventory-readonly`  | `/mcp/inventory_readonly`        | `eager`   |
| `toolbox-cross-catalog`       | `/mcp/cross_catalog`             | `lazy`    |

`eager` servers auto-connect when Pi starts; `lazy` ones connect
only on demand (Pi prompts before activating them). Drop any entry
you don't want; the `analytics_dev` toolset (agent-supplied SQL)
is intentionally not in the example — keep that one out of any
agent surface.

### Scoping any MCP client to a single toolset

The path-based routing isn't Pi- or Claude-specific. Toolbox
exposes `/mcp` (the default toolset = every registered tool) and
`/mcp/<toolset_name>` (just that toolset's tools); any MCP
client — Claude Code, Pi, LangChain's MCP adapter, the MCP
Inspector — can be scoped to a single toolset just by pointing it
at the appropriate URL. For agents that work better with a small
focused tool list (most do), prefer the scoped endpoints over the
catch-all `/mcp`.

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
├── docker-compose.yaml         # always-on: quack-server, quack-server-2, toolbox, otel-collector, jaeger, cube, cube-seed, superset
│                               # profile-gated: trace-client, trace-load, langgraph, inspector
├── tools.yaml                  # MCP Toolbox source + tool config (6 toolsets, 3 sources)
├── quack-server/
│   ├── Dockerfile              # Debian + DuckDB CLI + Quack
│   ├── entrypoint.sh           # envsubst init.sql.tmpl, then duckdb
│   ├── init.sql.tmpl           # INSTALL/LOAD quack, seed, authz, serve
│   ├── seed.sql                # baked into image — sales + orders (~30 rows)
│   ├── seed-inventory.sql      # mounted over seed.sql by quack-server-2 — products (~20 rows) + parquet view
│   ├── gen_price_history.py    # reproducible generator for the parquet fixture
│   └── product_price_history.parquet  # mounted into quack-server-2, exposed as a view in `main`
├── otel-collector/
│   └── config.yaml             # OTLP receivers + jaeger forwarder + debug exporter
├── trace-client/               # profiles: trace + load  (Python, no LLM)
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── app.py                  # Manual _meta.traceparent injection demo (one call)
│   └── load_test.py            # Concurrent fan-out across both sources + Jaeger verifier
├── langgraph/                  # profile: agent  (Anthropic ReAct agent)
│   ├── Dockerfile              # python:3.12-slim
│   ├── pyproject.toml          # toolbox-langchain + langgraph + langchain
│   └── app.py                  # ReAct agent with telemetry_enabled=True
├── notebooks/
│   └── walkthrough.ipynb       # 40-cell interactive tour of the whole demo
├── claude-code/
│   └── claude_config.example.json   # default /mcp (all tools); swap path to scope
├── pi/
│   └── pi_config.example.json       # four servers, each scoped to one toolset
├── cube/                             # Cube Core semantic layer
│   ├── model/cubes/                  # sales.yml + orders.yml (measures + dimensions)
│   ├── codegen.yml                   # slice list — which (cube × measures × dimensions) become tools
│   └── gen_toolbox_from_cube.py      # cube YAML + codegen.yml → cube_* entries in tools.yaml
├── superset/                          # Apache Superset BI sidecar (Cube → Superset → dashboard)
│   ├── superset_config.py            # SECRET_KEY, ROW_LIMIT, Talisman off for the demo
│   ├── bootstrap.sh                  # idempotent: db upgrade + create-admin + init + gunicorn
│   ├── bootstrap.py                  # REST-API bootstrap: database + dataset + chart + dashboard
│   └── screenshot.py                 # Playwright capture → docs/superset_dashboard.png
├── docs/
│   └── stack.svg               # architecture diagram (rendered at the top of this README)
├── .env.example
├── NOTES.md                    # paste-ready upstream bug writeups
└── README.md
```

## License

Apache 2.0 (matches the upstream MCP Toolbox project).
