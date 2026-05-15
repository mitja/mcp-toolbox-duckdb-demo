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

## LangGraph agent demo

```bash
docker compose --profile agent run --rm langgraph
```

The agent loads the `analytics_readonly` toolset over HTTP, then asks Claude
to summarize revenue for customers matching "gmbh". It prints the
intermediate tool calls and the final answer.

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
