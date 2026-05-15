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
