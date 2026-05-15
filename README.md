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
Toolbox is reachable on `localhost:5000`. Smoke-test the tool:

```bash
# List available tools via the native API
curl -s http://localhost:5000/api/tools | jq .

# Invoke the curated revenue tool
curl -s -X POST http://localhost:5000/api/tool/revenue_by_customer/invoke \
    -H 'Content-Type: application/json' \
    -d '{"customer_pattern": "gmbh"}' | jq .
```

You should get back a JSON response shaped like
[spec §7](https://github.com/mitja/mcp-toolbox-duckdb/blob/feat/duckdb-quack/.spec/mcp-toolbox-quack-duckdb.md#7-result-format):
typed columns, ordered rows, row count, truncation flag, and a statement
hash.

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
`./.mcp.json`). With the Compose stack running on `localhost:5000`, Claude
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
  bootstrapped with. Both services read it from `.env` via Compose; check
  that you don't have a stale Quack container with the previous token.
  `docker compose down && docker compose up --build`.
- **`tail -f /dev/null | duckdb` exits immediately**: the DuckDB CLI in the
  image does not support `quack`. Confirm the `DUCKDB_VERSION` build arg in
  `quack-server/Dockerfile` matches a release where Quack is bundled in
  `core_nightly` (currently v1.5.2+).
- **LangGraph container fails on `import toolbox_langchain`**: the demo
  pins `toolbox-langchain>=0.4.0`. If your local PyPI mirror is older,
  override with `pip install --upgrade toolbox-langchain` in the
  Dockerfile or pin a specific version.

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
