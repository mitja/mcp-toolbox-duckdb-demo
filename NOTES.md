# Notes

Things discovered while building this demo that are worth flagging
upstream (or at least worth knowing if you hit them yourself).

## Quack URI parser confuses hostname == scheme keyword

**Status:** Reproduced on DuckDB v1.5.2 + Quack (`core_nightly`) as of
May 2026. Not yet filed upstream.

**Symptom:** `ATTACH 'quack:quack:9494' AS remote (TYPE quack)` fails
with `IO Error: Failed to send message: IO Error: Timeout was reached
error for HTTP POST to 'http://9494:9494/quack'`. The Quack client is
trying to connect to port `9494` *on host* `9494`, not to the host named
`quack`.

**Cause:** Quack's URI parser treats `quack:` as the scheme prefix and
then re-scans the remainder. When the remainder starts with the
literal `quack`, the parser confuses it with another scheme prefix and
shifts the parse: the next token becomes the host *and* the token
after that becomes the host again. Hostnames that just *contain*
`quack` (e.g., `quack-server`, `myquack`) parse correctly.

**Reproducer (Go):**

```go
db, _ := sql.Open("duckdb", "")
db.ExecContext(ctx, "INSTALL quack FROM core_nightly")
db.ExecContext(ctx, "LOAD quack")

// Inspect the parser's output for various hostnames.
for _, uri := range []string{
    "quack:quack:9494",       // collides
    "quack://quack:9494",     // also collides
    "quack:quack-server:9494",
    "quack:localhost:9494",
} {
    var s string
    db.QueryRowContext(ctx,
        "SELECT quack_uri_parser($1, false)::VARCHAR", uri).Scan(&s)
    fmt.Printf("%-32q -> %s\n", uri, s)
}
```

Output:

```
"quack:quack:9494"               -> {'host': 9494, 'port': 9494, 'ipv6': false, 'ssl': false, 'url': 'http://9494:9494'}
"quack://quack:9494"             -> {'host': 9494, 'port': 9494, 'ipv6': false, 'ssl': false, 'url': 'http://9494:9494'}
"quack:quack-server:9494"        -> {'host': quack-server, 'port': 9494, 'ipv6': false, 'ssl': false, 'url': 'http://quack-server:9494'}
"quack:localhost:9494"           -> {'host': localhost, 'port': 9494, 'ipv6': false, 'ssl': false, 'url': 'http://localhost:9494'}
```

**Workaround (used by this demo):** Do not name the Quack-serving
docker service `quack`. We use `quack-server` so the resolved URI
becomes `quack:quack-server:9494`, which parses correctly. The same
applies to any DNS hostname, Kubernetes service name, or
`/etc/hosts` alias.

**To file upstream:** the canonical place is
<https://github.com/duckdb/duckdb> with the `quack` label. Title:
`Quack URI parser: hostname 'quack' is interpreted as the scheme and
shifts the parse`. Include the four-line reproducer above, the
expected vs. observed `quack_uri_parser` output, and the DuckDB +
Quack versions (`SELECT version()`, `SELECT extension_version FROM
duckdb_extensions() WHERE extension_name = 'quack'`).

## `quack_authorization_function` rejects ATTACH's own catalog probes

**Status:** Reproduced on DuckDB v1.5.2 + Quack (`core_nightly`) as of
May 2026. Not yet filed upstream.

**Symptom:** Configure a typical "read-only" Quack authorization macro
(returns `true` iff the query starts with one of
`SELECT|WITH|EXPLAIN|DESCRIBE|SHOW`), activate it with
`SET GLOBAL quack_authorization_function = 'read_only'`, then connect
a fresh client. The client's first `ATTACH 'quack:host:port' AS remote
(TYPE quack)` fails with `Invalid Input Error: Authorization failed`,
even though the client has not yet run any user query.

**Cause:** Quack invokes the authorization callback for *every* query
the server receives — including the internal catalog probe queries
that `ATTACH` itself issues to enumerate schemas and tables. Some of
those probes don't begin with one of the whitelisted leading keywords
(the exact set depends on the DuckDB/Quack version), so the `read_only`
macro returns `false` and the connection's setup fails before the
client can run a real query.

**Reproducer (Go, in-process):**

```go
ctx := context.Background()

// Server side: serve + activate read_only authz BEFORE any client connects.
srv, _ := sql.Open("duckdb", "")
defer srv.Close()
srv.SetMaxOpenConns(1)
for _, s := range []string{
    "INSTALL quack FROM core_nightly",
    "LOAD quack",
    `CREATE MACRO read_only(sid, query) AS (
        regexp_matches(upper(trim(query)), '^(SELECT|WITH|EXPLAIN|DESCRIBE|SHOW)\b'))`,
    "SET GLOBAL quack_authorization_function = 'read_only'",
    "CALL quack_serve('quack:127.0.0.1:9494', token := 'tok12345', " +
        "allow_other_hostname := true, disable_ssl := true)",
} {
    if _, err := srv.ExecContext(ctx, s); err != nil { log.Fatal(err) }
}

// Client side: ATTACH fails.
cli, _ := sql.Open("duckdb", "")
defer cli.Close()
cli.ExecContext(ctx, "INSTALL quack FROM core_nightly")
cli.ExecContext(ctx, "LOAD quack")
cli.ExecContext(ctx, "CREATE SECRET s (TYPE quack, TOKEN 'tok12345')")
_, err := cli.ExecContext(ctx,
    "ATTACH 'quack:127.0.0.1:9494' AS remote (TYPE quack, DISABLE_SSL true)")
fmt.Println(err) // Invalid Input Error: Authorization failed
```

Reorder the server-side block so `SET GLOBAL quack_authorization_function`
runs *after* the client's `ATTACH` succeeds, and everything works:
queries the client subsequently runs are filtered by `read_only` as
expected, and destructive statements get the rejection the macro is
there to provide.

**Workaround (used by this demo):** `quack-server/init.sql.tmpl`
creates the `read_only` macro but does **not** activate it via
`SET GLOBAL`. The README has an "Enabling server-side authz" section
that documents the manual post-startup step:

```bash
docker exec duckdb-quack duckdb /data/analytics.duckdb -cmd \
  "SET GLOBAL quack_authorization_function = 'read_only'" -cmd ".quit"
```

This is acceptable for the demo because layer 1 (the Toolbox tool's
config-load validator) and layer 2 (the source's `policy.timeout` /
`policy.max_rows`) already cover the same destructive-statement
surface; layer 3 is a backstop against bugs and future raw-SQL tool
surfaces.

**Why it's awkward and worth filing:** the natural deployment shape
is "bring up the server fully configured, then bring up clients."
Quack's model inverts that — clients must `ATTACH` first, then someone
activates authz, and the activation must somehow not break *future*
clients that reconnect after a network hiccup or a Toolbox restart.
A cleaner design would either:

- explicitly allowlist the catalog-probe statements `ATTACH` issues
  (so a query-text-based macro can be written to pass them), **or**
- expose a per-client-lifecycle hook (e.g., `quack_authorization_function`
  only invoked *after* the attach handshake completes), **or**
- pass a context flag to the callback indicating "this is an internal
  probe, not user-issued" so the macro can decide separately.

**To file upstream:** same place as the URI parser issue
(<https://github.com/duckdb/duckdb>, `quack` label). Title:
`quack_authorization_function rejects ATTACH's internal catalog
probes, making it impossible to enable authz before any client
connects`. Attach the Go reproducer above, the exact error message,
the DuckDB + Quack versions, and a screenshot or `EXPLAIN`-style
trace of which internal queries are actually being rejected if you
can capture it (`SET GLOBAL quack_log_level = 'DEBUG'` may help).

## `--telemetry-otlp` expects host:port, not a URL, and silently HTTPS+gRPC

**Status:** Reproduced on Toolbox v1.2.0+dev (the
`googleapis/mcp-toolbox` repo, HEAD as of May 2026). Not yet filed
upstream.

**Symptom:** Toolbox's CLI help advertises:

```
--telemetry-otlp string    Enable exporting using OpenTelemetry
                           Protocol (OTLP) to the specified endpoint
                           (e.g. 'http://127.0.0.1:4318')
```

Passing `http://otel-collector:4318` (a URL, as the help suggests)
crashes at startup:

```
ERROR error setting up OpenTelemetry: unable to set up meter provider:
parse "https://http:%2F%2Fotel-collector:4318/v1/metrics":
invalid URL escape "%2F"
```

Stripping the scheme to `otel-collector:4318` gets past startup but
the spans/metrics still don't reach a plaintext-HTTP collector:

```
traces export: Post "https://otel-collector:4318/v1/traces":
http: server gave HTTP response to HTTPS client
```

**Cause (two layers):**

1. The CLI calls
   `otlpmetrichttp.New(ctx, otlpmetrichttp.WithEndpoint(telemetryOTLP))`
   and the equivalent for traces (see
   `internal/telemetry/telemetry.go`). `WithEndpoint` expects a
   `host:port` string and the SDK prepends `https://<host>/v1/{...}`
   itself. Passing a URL doubles the scheme.

2. Even with a bare `host:port`, the SDK default scheme is HTTPS and
   the default transport for the equivalent `otlptracehttp` /
   `otlpmetrichttp` is HTTP (different from the SDK's *gRPC* default
   when the user picks the wrong package, but the point is: TLS is
   on unless told otherwise). For an in-cluster collector that does
   not terminate TLS, the operator must set
   `OTEL_EXPORTER_OTLP_INSECURE=true`, and to match a host:port HTTP
   collector the operator typically also wants
   `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. Neither is mentioned
   in the CLI help or the docs.

**Reproducer:**

```bash
# Start a debug-exporter collector listening on plaintext HTTP 4318:
docker run --rm -p 4318:4318 -p 4317:4317 \
    -v "$PWD/otel-collector/config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
    otel/opentelemetry-collector-contrib:0.115.1 \
    --config=/etc/otelcol-contrib/config.yaml &

# Form (a): URL as the help suggests — Toolbox refuses to start.
toolbox --config tools.yaml --telemetry-otlp http://127.0.0.1:4318

# Form (b): host:port — Toolbox starts, but the exporter sends
# HTTPS to the plain-HTTP collector, telemetry never lands.
toolbox --config tools.yaml --telemetry-otlp 127.0.0.1:4318

# Form (c): host:port + the two env vars — works.
OTEL_EXPORTER_OTLP_INSECURE=true \
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
    toolbox --config tools.yaml --telemetry-otlp 127.0.0.1:4318
```

**Workaround (used by this demo):** `--telemetry-otlp
otel-collector:4318` on the CLI plus
`OTEL_EXPORTER_OTLP_INSECURE=true` and
`OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` in `environment:` on the
toolbox service. See `docker-compose.yaml` and the README's
"Observability" section for the full block.

**To file upstream:** <https://github.com/googleapis/mcp-toolbox>.
Title: `--telemetry-otlp accepts host:port but the CLI help suggests
a URL, and TLS is on by default with no flag to disable it`. Two
plausible fixes the issue should propose:

1. **Docs-only**: update the help string and a docs page to say
   `host:port (e.g. otel-collector:4318)`, document the two
   `OTEL_EXPORTER_OTLP_*` env vars, and remove the misleading
   `http://...` example.

2. **Code**: accept a full URL on `--telemetry-otlp`. If the value
   parses as a URL with a scheme, drive the exporter from the parsed
   components (`http` → `WithInsecure()`, host:port from
   `URL.Host`); else keep current host:port behavior. Less surprise
   for operators copy-pasting from any OTLP doc on the internet.

Attach the three reproducer forms above and the version probe
(`toolbox --version`).

## Toolbox-server: also extract `traceparent` from HTTP headers, not only from MCP `_meta`

**Status:** Reproduced on Toolbox v1.2.0+dev as of May 2026. Not yet
filed upstream. The SDK-side gap that originally motivated this
writeup has since been fixed by `toolbox-langchain >= 1.0` /
`toolbox-core >= 1.0` (which inject `_meta.traceparent` automatically
when `ToolboxClient(..., telemetry_enabled=True)`), so the demo's
agent → toolbox → quack trace stitches end-to-end today. The
server-side gap below is still worth filing as a defense-in-depth.

**Symptom (server side, still present):** A client that hits the
REST `/api/tool/<name>/invoke` endpoint with W3C `traceparent` set
**as an HTTP header** (the OTel default everywhere outside MCP) sees
its toolbox-side spans land under a fresh trace ID, disjoint from
the caller's trace. Same applies to any MCP SDK that hasn't adopted
the `_meta.traceparent` convention from the MCP 2025-06-18 spec yet.

**Cause:** Toolbox extracts incoming W3C TraceContext exclusively
from the JSON-RPC `params._meta.traceparent` field (see
`internal/server/mcp.go` line ~150, function `extractTraceContext`),
not from the HTTP request headers. The `/api` routes (REST tool
invoke, toolset listing) have no extraction at all.

**Reproducer:**

```bash
# Bring up the OTel-instrumented stack
docker compose up -d jaeger otel-collector quack-server toolbox

# Post to the REST endpoint with an explicit traceparent header.
TP="00-$(openssl rand -hex 16)-$(openssl rand -hex 8)-01"
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "traceparent: $TP" \
  http://localhost:5555/api/tool/revenue_by_customer/invoke \
  -d '{"customer_pattern":"gmbh"}' > /dev/null

# Look up the trace ID Toolbox produced. It does NOT match $TP.
curl -s "http://localhost:16686/api/traces?service=duckdb-quack-demo&limit=1&lookback=2m" \
  | jq -r '.data[0].traceID'
echo "client trace_id: $(echo $TP | cut -d- -f2)"
```

The two trace IDs differ; the toolbox span tree is rooted on a
fresh trace.

**Workaround for REST callers:** Switch the call to the MCP
`/mcp` endpoint and embed `traceparent` in `_meta` (see
[`trace-client/`](trace-client/) for a minimal example).

**To file upstream — `googleapis/mcp-toolbox`.** Title:
`Also extract traceparent from HTTP headers, not only from MCP _meta`.
The change is to wrap the chi router with `otelhttp.NewHandler(...)`
(or call
`otel.GetTextMapPropagator().Extract(ctx, propagation.HeaderCarrier(r.Header))`
in a middleware) before the existing `extractTraceContext`. This
enables trace propagation for both REST callers and any future
MCP SDK that hasn't adopted `_meta.traceparent` yet. The change is
non-invasive: it adds a primary extraction at the HTTP layer; the
existing `_meta` extraction stays authoritative since it runs
later and can override.

**Related, resolved:** the client-side counterpart (SDK should
inject `_meta.traceparent`) was fixed in `toolbox-langchain` and
`toolbox-core` 1.0 — the new `telemetry_enabled=True` constructor
arg activates it. No upstream report needed.

## `/api/tool/<name>/invoke` returns empty 200 when `Content-Type` is missing

**Status:** Reproduced on Toolbox v1.2.0+dev as of May 2026. Not yet
filed upstream.

**Symptom:** Posting a JSON body to a tool's REST invoke endpoint
without `Content-Type: application/json` succeeds with HTTP 200 but
returns an **empty response body**. The tool never actually runs —
nothing in toolbox logs at INFO/DEBUG, no `duckdb.query` span in
the OTel collector, no row data. The user sees `jq: error (at
<stdin>:0): Cannot index empty string` (or silence) and has no
hint about what went wrong.

It bit this demo's README when the metadata-tool curl examples
omitted the header for a parameterless tool. Curl's `-d` flag
defaults the Content-Type to `application/x-www-form-urlencoded`
when no `-H` is set, and Toolbox's request handler treats that as
a body it cannot parse — but returns success anyway.

**Cause:** Toolbox's `/api/tool/.../invoke` handler decodes the
request body as JSON only when the Content-Type advertises that.
For other content types (the curl-bare-`-d` default included), the
handler short-circuits without invoking the tool. The 200 response
appears to come from a successful but no-op code path rather than
from a 400/415 rejection.

**Reproducer:**

```bash
# WITHOUT the header — HTTP 200, empty body, tool does not run
curl -sS -i -X POST http://localhost:5555/api/tool/list_catalogs/invoke -d '{}'

# WITH the header — HTTP 200, spec §7 JSON envelope, tool runs
curl -sS -X POST http://localhost:5555/api/tool/list_catalogs/invoke \
    -H 'Content-Type: application/json' -d '{}' \
  | jq '.result | fromjson | {row_count, catalogs: [.rows[].catalog_name]}'
```

The two requests differ only in the header.

**Workaround:** Always send `Content-Type: application/json`.
Higher-level clients usually do this automatically (Python's
`requests.post(url, json=...)`, JavaScript `fetch` with `JSON.stringify`
+ explicit header, Go's `net/http` with `Set("Content-Type", ...)`),
so this is mostly a footgun for shell users hand-rolling curl
invocations. Updated this repo's README to set the header in every
example after rediscovering the issue.

**To file upstream:** <https://github.com/googleapis/mcp-toolbox>.
Title: `POST /api/tool/<name>/invoke returns empty 200 instead of
400/415 when Content-Type is not application/json`. Three plausible
fixes the issue should propose:

1. **Strict:** return HTTP 415 (Unsupported Media Type) when the
   request has a body but no acceptable Content-Type. Standard and
   loud — the caller sees the problem immediately. Slight backwards-
   incompatibility risk for any caller that was relying on the
   silent-no-op behavior (unlikely but possible).

2. **Permissive:** sniff the body. If it parses as JSON regardless
   of the declared Content-Type, run the tool. Easy on shell users;
   adds a tiny bit of magic that may surprise readers of the code.

3. **Loud no-op:** keep accepting the request but write a WARN log
   line (`"empty body parse: content-type %q not handled"`) so the
   silent-no-op is at least discoverable from the server side.
   Minimal blast radius, surfaces the issue without breaking
   anything.

Of the three, **(1)** is the cleanest API-design choice and matches
how every other HTTP framework handles a non-JSON body on a JSON
endpoint. **(3)** is the least disruptive if backwards compatibility
is a hard constraint. Attach the two-curl reproducer above and the
toolbox version (`toolbox --version`).

## Feature idea: record in-process DuckDB memory usage as OTel data

**Status:** Not yet implemented. Improvement for the fork's
duckdb-quack adapter; would land in
`internal/sources/duckdbquack/instrumentation.go`.

**Motivation:** Today the source emits five metrics around the
`duckdb.query` span (duration, rows returned, errors, truncated,
reattach) but says nothing about how much memory the in-process
DuckDB is using or how close it is to its `memory_limit`. With
multi-attach in place, the local-side buffer pool now matters
more — cross-catalog joins (e.g. `product_orders_overview` in the
demo) materialize both inputs locally, so an operator wants to
graph "buffer pool used vs. limit" and "which queries grow the
pool" without poking at DuckDB by hand.

**Mechanism (already in DuckDB):** the `duckdb_memory()` table
function returns per-subsystem usage:

```sql
SELECT tag, memory_usage_bytes, temporary_storage_bytes FROM duckdb_memory();
-- BUFFER_MANAGER | 3145728 | 0
-- HASH_TABLE     |  524288 | 0
-- ORDER_BY       |       0 | 0
-- ...
```

Plus `current_setting('memory_limit')` for the configured cap (a
static value at any given moment).

**Two recording shapes worth considering:**

1. **OTel `ObservableGauge` per source** (`duckdb.memory.bytes`).
   A callback runs the introspection query on each metric export
   (default 60s). Dimensions: `toolbox.source.name`, optionally
   `tag` for the per-subsystem breakdown. Operators graph
   `memory.bytes / memory_limit.bytes` to see headroom over time.

2. **Span attributes on every `duckdb.query`**
   (`db.memory.bytes.before` / `after` /
   `db.temporary_storage.bytes`). Per-query memory deltas show up
   directly in Jaeger so you can see exactly which queries grow
   the buffer pool or spill to disk. Cost: two extra small SELECTs
   per tool invocation.

**Specific gotcha for this adapter:** each duckdb-quack source has
`SetMaxOpenConns(1)` because the ATTACH state lives on a single
connection. A scheduled metric scrape competes with the user query
for that conn. Three options:

- (a) Short timeout + drop the scrape on contention (best-effort
  observability).
- (b) Bump the pool to 2 and re-run `LOAD quack` + `CREATE SECRET`
  + `ATTACH` on the second conn too — including the reconnect
  path. More work, more state.
- (c) Skip the scheduled gauge entirely; only sample from inside
  `RunSQL` as span attributes (so the conn-contention question
  doesn't arise — the connection is already held for the user
  query).

**Recommendation:** start with **option 2 (span attributes), not the
gauge.** Lower effort, inherits the existing reconnect/timeout
discipline, and it's the answer to the question operators usually
actually have ("which queries grew the buffer pool?"). Include
`temporary_storage_bytes` so spill-to-disk events show up clearly
in Jaeger. Also emit a one-shot `db.memory_limit.bytes` as a
resource attribute (or per-source attribute on the first span) so
the ratio is computable downstream.

Add the periodic ObservableGauge later if a real need for
time-series memory dashboards independent of query activity shows
up — at that point pick option (a) or (b) based on whether the
contention-induced gaps in the gauge are acceptable.

**Suggested attribute names** (subject to OTel semconv updates;
follow `db.*` and `db.temporary_storage.*` if a standard appears):

- `db.memory.bytes.before` — `SUM(memory_usage_bytes)` snapshot
  before the query runs.
- `db.memory.bytes.after` — same snapshot after the query
  completes (success path; on failure, capture before the error
  return).
- `db.temporary_storage.bytes` — `SUM(temporary_storage_bytes)`
  delta over the query (or just `.after`; the value is monotonic
  per query but the buffer pool is not).
- `db.memory_limit.bytes` — `current_setting('memory_limit')` as
  bytes, read once at source init and attached as a static span
  attribute (or `Resource` attribute on the tracer provider).

## Feature idea: federate across non-Quack backends via the in-process DuckDB

**Status:** Not yet implemented. Improvement for the fork's
duckdb-quack adapter; would generalize `additional_attachments` in
`internal/sources/duckdbquack/duckdbquack.go`.

**Motivation:** The multi-attach work we just landed lets one
duckdb-quack source ATTACH several Quack servers and JOIN across
them inside the in-process DuckDB. But the in-process DuckDB is
not Quack-specific — DuckDB ships first-class extensions for
talking to Postgres, MySQL, SQLite, Iceberg, Delta, S3, plus
direct Parquet / CSV reads. If `additional_attachments` accepted
those URIs too, a single `duckdb-sql` tool could JOIN a
Quack-served fact table with a Postgres lookup table, an Iceberg
snapshot in object storage, or a CSV in S3 — all optimized and
executed by the in-process DuckDB.

This is the same architectural pattern as multi-attach, just
without the constraint that every attachment be a Quack server.

**MCP Toolbox layer cannot federate across source types.** Each
Toolbox tool is bound to one source (a `postgres-sql` tool talks
to a `postgres` source, etc.); the tool executor only knows its
one source. Federation has to happen *inside* a source — the
duckdb-quack source's in-process DuckDB is the natural place for
it because DuckDB is a real query engine, not just a connector.

**What the adapter would need:**

1. Generalize `additional_attachments` to accept arbitrary URI
   schemes + per-type ATTACH options. Today validation forces a
   `quack:` prefix; that becomes a per-type validator dispatched
   by a new `type:` field (or by URI scheme).
2. Per-type secret creation. Quack secrets use `(TYPE quack, TOKEN
   '…')`; Postgres uses `(TYPE postgres, USER '…', PASSWORD '…',
   HOST '…')`; S3 uses `(TYPE s3, KEY_ID '…', SECRET '…', REGION
   '…')`. Each backend has its own field schema and its own
   character-set / escaping concerns.
3. INSTALL/LOAD per extension at source init (`INSTALL postgres
   FROM core; LOAD postgres`). Avoid double-installing the same
   extension across two attachments in the same source.
4. Reconnect heuristics expanded. The current `needsReAttach`
   substring matchers (`Invalid connection id`, "Failed to send
   message", etc.) are Quack-specific. Each extension has its own
   way of surfacing "remote is gone" — e.g., Postgres returns
   `FATAL: terminating connection`. The retry path either needs
   per-type matchers or a more conservative "always retry on
   driver.ErrBadConn" stance for non-Quack attachments.

**Trade-offs vs. our current Quack-only setup:**

- **Pushdown varies wildly by extension.** Postgres scanner pushes
  filters down reasonably; SQLite, CSV, and Parquet readers are
  mostly full-scan + local filter. Cross-source joins where one
  side does not push will pull a lot more across the wire.
- **No server-side authz outside Quack.** We currently rely on
  Quack's `read_only` macro as the real boundary. For Postgres /
  MySQL you'd lean on the database user's `GRANT` privileges,
  which the adapter cannot enforce — `policy.read_only` becomes
  informational again.
- **Memory pressure goes up.** Cross-source joins are
  local-execution by definition (different physical backends), so
  the Toolbox-side DuckDB has to materialize at least one side.
  The `policy.max_rows` cap helps but is downstream of the join.
- **Schema validation gets richer.** The `additional_attachments`
  YAML shape is currently a small struct; with per-type options
  it grows to a tagged union. Worth a careful design pass on the
  ergonomics.

**Recommendation:** treat this as a separate, later phase. Start
with **one extension — Postgres** — because it is the most
commonly requested federation target and has the best pushdown of
the DuckDB extensions. Generalize the URI / secret plumbing for
that single case first; once it works, adding MySQL / SQLite /
Iceberg / S3 is mostly a matter of writing more per-type secret
templates and validators. Do not try to generalize speculatively
before having one working case.

**Suggested config shape (illustrative):**

```yaml
sources:
  combined-analytics:
    type: duckdb-quack
    uri: quack:sales-server:9494
    token: ${QUACK_TOKEN}
    attach_alias: sales_remote
    additional_attachments:
      - type: quack
        uri: quack:inventory-server:9494
        attach_alias: inventory_remote
      - type: postgres
        uri: "host=lookups-pg dbname=ref user=ro password=${PG_PW}"
        attach_alias: lookup
        extension:
          install_from: core
```

The primary attachment stays as today (top-level fields) for
backward compatibility; `additional_attachments` becomes the
heterogeneous list where each entry's `type:` selects the secret
template and ATTACH option set.

## Pattern: federate non-DuckDB backends *inside* the remote Quack server

**Status:** Not a feature to build; a deployment pattern worth
recording. No code changes in the fork required — the Quack
server's own DuckDB does the work.

**Motivation:** The previous note ("federate across non-Quack
backends via the in-process DuckDB") considers federating from the
Toolbox-side adapter. The opposite shape is to push the
heterogeneous attachments *down* into the remote Quack server's
DuckDB — anything DuckDB can ATTACH, the Quack server can ATTACH
— and then the Toolbox client sees one unified `quack:` endpoint.
This is often the better architectural fit and needs zero adapter
code changes today.

**How it looks on the Quack server side** (illustrative, lives in
the operator's Quack `init.sql`, not in this repo):

```sql
INSTALL postgres FROM core;
LOAD postgres;
ATTACH 'host=lookups-pg user=ro password=...' AS pg_lookups (TYPE postgres);

INSTALL iceberg FROM core;
LOAD iceberg;
ATTACH 's3://bucket/warehouse/' AS lake (TYPE iceberg);

-- then start serving as usual
CALL quack_serve('quack:0.0.0.0:9494', token := '...', ...);
```

**On the Toolbox client side:** nothing changes. One
`ATTACH 'quack:remote:9494' AS remote` brings in everything the
remote server has, including the federated catalogs. A
`duckdb-sql` tool can then JOIN across them in one statement:

```sql
SELECT s.customer, l.region, SUM(s.amount) AS revenue
FROM remote.sales s
JOIN remote.pg_lookups.public.customer_region l ON l.customer = s.customer
GROUP BY s.customer, l.region
ORDER BY revenue DESC
```

**Why this is often the preferable shape:**

- **One ATTACH on the client.** No adapter changes — the
  multi-attach work already covers the multi-server case, and
  this puts the heterogeneous-backend complexity on the server
  side where it belongs.
- **Authz stays unified.** The `quack_authorization_function`
  (`read_only` macro) runs at the Quack server's SQL boundary, so
  it sees and authorizes *all* queries including those that reach
  into the attached Postgres / Iceberg / etc. One enforcement
  point for everything; no per-backend ACL juggling on the client.
- **Pushdown stacks naturally.** The client DuckDB pushes filters
  to Quack; Quack's DuckDB pushes the per-extension filters into
  the backend (Postgres / Iceberg / S3). Each hop's optimizer
  cooperates — assuming the underlying extension supports it
  (Postgres yes; CSV / Parquet less so).
- **No new secret types in the Toolbox client.** Backend
  credentials live on the Quack server's machine. The Toolbox
  process only ever sees `(TYPE quack, TOKEN '…')` and never
  handles Postgres / S3 credentials directly.

**Caveats worth knowing:**

- **Quack needs to expose nested catalogs cleanly.** Not
  rigorously confirmed: DuckDB's catalog model surfaces every
  attached DB under its alias, so `remote.pg_lookups.public.users`
  *should* be reachable through Quack — but Quack's catalog
  enumeration may only advertise the remote's `main` database. If
  that turns out to be the case, the workaround is to expose
  backend tables as **views** in the remote's `main` schema:

  ```sql
  CREATE VIEW main.users AS SELECT * FROM pg_lookups.public.users;
  ```

  Views in `main` are always advertised through Quack, and the
  view body still pushes down to Postgres at execution time. File
  a Quack issue if nested-catalog enumeration is broken — that's
  the cleaner long-term fix.

- **Pushdown is not guaranteed at every hop.** A complex predicate
  that DuckDB can push directly to Postgres might not survive the
  Quack hop. `EXPLAIN` on the client side shows whether the filter
  made it across.

- **You lose the per-backend process isolation** you get by
  running separate Quack servers. One slow Postgres query on the
  federated server contends with the in-process DuckDB's buffer
  pool and CPU for *every* query against that Quack server.

- **Authentication for the backend lives in the Quack server's
  init**, not in `tools.yaml`. That's fine for credentials that
  should not leave the data-plane machine, but means the
  Toolbox-side config can no longer document the full data lineage
  on its own — operators have to look in two places.

**Comparison with the local-side alternative:**

| Pattern                                         | Where federation runs       | Adapter changes needed |
|-------------------------------------------------|-----------------------------|------------------------|
| Multi-attach (shipped)                          | Toolbox-side DuckDB         | None (already shipped) |
| Heterogeneous local attachments                 | Toolbox-side DuckDB         | Generalize `additional_attachments` (NOTES entry above) |
| **Heterogeneous remote attachments via Quack**  | **Quack server-side DuckDB** | **None — server-side init.sql change only** |

**Recommendation for the demo / docs:** when this pattern comes
up in practice, prefer the server-side ATTACH path unless the
operator has a hard reason to do federation on the Toolbox side
(e.g., the Toolbox box has credentials the Quack box doesn't, or
the federation needs to happen across multiple Quack servers each
without write access to the others' init.sql). If we ever add a
demo for this, a third `quack-server-3` with a tiny Postgres
sidecar ATTACHed would be the minimum-viable example.

## DuckDB: "Multiple streaming scans not currently supported" blocks any join across two ATTACHed remotes

**Status:** Active limitation in DuckDB core (v1.5.2, observed
2026-05-18). Hit when we wired up the `current_prices` tool that
joins `inventory_remote.products` with the parquet-backed view
`inventory_remote.product_price_history` on the same Quack source.
The underlying DuckDB fix is still pending upstream. The two
adapter-side improvements documented in this entry are **shipped**
in the fork (`feat/duckdb-quack`):

- `push_down_to_remote: true` on `duckdb-sql` tools — see
  `internal/tools/duckdb/duckdbsql/duckdbsql.go` and the
  `TestInitialize_PushDownToRemote_*` tests. Used in the demo's
  `current_prices` tool.
- Friendlier streaming-scan error in `RunSQL` — see
  `internal/sources/duckdbquack/duckdbquack.go` (`wrapKnownErrors`)
  and `wrap_errors_internal_test.go`.

**Note on reproducibility.** Whether the error actually fires
depends on DuckDB's plan choice, which appears to be cost-sensitive.
The same SQL that failed during initial integration sometimes plans
into a non-streaming shape on later runs of the same data. The
unit-test pinning in the fork's wrap_errors test is the load-bearing
verification for the friendlier-error behavior; the
`push_down_to_remote` flag is the guaranteed workaround for the
case where DuckDB does pick the streaming plan.

**Symptom.** A `duckdb-sql` tool whose SQL references **two or more**
Quack-ATTACHed tables in the same physical plan fails at execution
with:

```
Not implemented Error: Multiple streaming scans or streaming scans +
CTAS / insert in the same query are not currently supported
```

Both single-source joins (two tables under one alias) and
multi-attach joins (one table per alias from `additional_attachments`)
trigger it. Single-table reads are fine. Materialization hints don't
help — `WITH … AS MATERIALIZED (…)` still has a streaming scan as
input, and DuckDB checks for the scan, not for the materialization
boundary.

**Reproducer** (against the demo's `inventory-quack` source):

```sql
-- fails with the error above
SELECT p.name, h.unit_price
FROM   inventory_remote.products p
LEFT JOIN inventory_remote.product_price_history h
       ON h.product_name = p.name AND h.valid_to IS NULL;
```

```sql
-- also fails; MATERIALIZED does not bypass the planner check
WITH curr AS MATERIALIZED (
  SELECT product_name, unit_price
  FROM inventory_remote.product_price_history
  WHERE valid_to IS NULL
)
SELECT p.name, c.unit_price
FROM   inventory_remote.products p
LEFT JOIN curr c ON c.product_name = p.name;
```

**Workaround we shipped** (visible in `tools.yaml` for `current_prices`):
push the whole join down to the remote via `quack_query()` so only
one streaming result-set scan comes back:

```sql
SELECT *
FROM quack_query(
  'quack:quack-server-2:9494',
  '
    SELECT p.name, h.unit_price
    FROM products p
    LEFT JOIN product_price_history h
      ON h.product_name = p.name AND h.valid_to IS NULL
  ',
  disable_ssl := true
);
```

Pros: works today, and incidentally executes the join next to the
data — usually faster. Cons: the URI is duplicated between the
source config and every affected tool; you lose the agent-readable
shape of "this tool joins these attached tables" in favor of an
opaque string; and it doesn't generalize to *cross-source* joins
(multi-attach), where there is no single remote you can push to.

**Where the underlying fix belongs.** Three candidates, ranked by
where the work would actually go:

1. **DuckDB core (`duckdb/duckdb`) — the right place; still open.**
   The error is raised by the streaming execution engine itself,
   not by any Quack-specific code. The Quack scanner is a perfectly
   ordinary `read_quack(...)`-style table function that yields rows
   lazily; the same constraint would bite any future remote-scan
   extension built the same way. Lifting it means letting the
   streaming scheduler drive more than one streaming source in a
   single pipeline (or auto-materializing the second one). This is
   a non-trivial planner/executor change, but it is the change with
   the broadest payoff — and the error message already reads like a
   known limitation that someone intends to revisit.
2. **Quack extension (`duckdb/duckdb-quack`) — wrong layer, but
   possible as a flag.** Quack could expose a "buffered" scan mode
   on the ATTACH that fully consumes the remote response into a
   local result set before yielding rows. That would sidestep the
   streaming-scan constraint, at the cost of the very property that
   makes Quack interesting (zero-materialization streaming). Worth
   filing only if the DuckDB core fix is years away.
3. **Our fork (`mitja/mcp-toolbox-duckdb`) — ergonomics, IMPLEMENTED.**
   We can't lift the planner restriction from outside DuckDB, but
   we shipped two adapter improvements that remove the "what is
   this error and how do I fix it?" cliff for the common
   single-source case:

   - **`push_down_to_remote: true`** on `duckdb-sql` tools. When
     the source is a `duckdb-quack` source and the tool has no
     bound `parameters:` (template parameters are fine — they
     substitute before the wrap), Invoke routes the statement
     through the source's `QuackQuery()` method instead of
     `RunSQL()`. The wrap, instrumentation, and reattach path are
     identical to the manual `quack_query()` approach we shipped
     in the demo first. Config-load checks reject the flag on
     non-quack sources (`source.SourceType()` not duckdb-quack) and
     on tools that mix it with bound parameters. See
     `internal/tools/duckdb/duckdbsql/duckdbsql.go` and the
     `TestInitialize_PushDownToRemote_*` tests in
     `initialize_test.go`. The demo's `current_prices` tool was
     converted from the hand-written `quack_query()` wrapper to
     this flag and works identically end-to-end.
   - **Friendlier streaming-scan error** in `RunSQL`. The
     `wrapKnownErrors` helper detects the
     `"Multiple streaming scans or streaming scans"` substring in
     a DuckDB error and rewrites the message to name both
     workarounds (`push_down_to_remote: true` for single-source,
     manual `quack_query('<primary-uri>', '<sql>', disable_ssl := …)`
     for multi-attach). The original error is preserved via `%w` so
     `errors.Is` / `errors.Unwrap` still work. Lives next to
     `needsReAttach` in `internal/sources/duckdbquack/duckdbquack.go`;
     pinned by `wrap_errors_internal_test.go`. Doesn't help the
     cross-source case at runtime (no single remote to push to),
     but the message at least points the operator at the manual
     `quack_query()` workaround.

   Neither solves the underlying limitation. The DuckDB core fix
   is still the right long-term resolution.

**Suggested upstream issue title** (file in `duckdb/duckdb`, not in
duckdb-quack):
*"Lift 'Multiple streaming scans … not currently supported' for table
functions in the same pipeline"* — include the reproducer above
against any pair of streaming-scan table functions (Quack is the
clearest demonstration but `read_parquet` parallel scans, the HTTP
fs scanner, etc. should reproduce the same way).

**Suggested upstream issue title** (file in `duckdb/duckdb-quack`,
lower priority): *"Optional buffered ATTACH mode to work around
'Multiple streaming scans not currently supported' in client
queries"* — only if the DuckDB core fix is not on the near-term
roadmap.
