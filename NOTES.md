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
