-- The Quack server's read-only authorization callback (shared by init.sql.tmpl and init-engineered.sql.tmpl).
--
-- Quack calls the callback with the session id and the query text, and runs the query the callback RETURNS
-- (VARCHAR); NULL refuses it with "Authorization failed". So:
--   * the macro returns the query, not a boolean: a boolean macro refuses EVERY request, ATTACH included (that is
--     what NOTES.md used to read as "ATTACH's catalog probes are rejected"; the probes are ordinary SELECTs that
--     start with a newline);
--   * the macro is persistent (CREATE MACRO, not TEMP): a temporary macro is invisible to the server's connections;
--   * DuckDB's parser decides, not a regex on the first keyword ("SELECT 1; DELETE FROM sales" passes such a regex
--     and empties the table): exactly one SELECT (json_serialize_sql serializes nothing else; DESCRIBE and SHOW parse
--     as one) that reads no file: no file-reader table function and no table named by a path (FROM '/etc/passwd').
--     Views over read_parquet (product_price_history, the engineered marts) still work: the client's query names
--     the view, the server expands it.
-- It is switched on before quack_serve, so it holds from the first client on.
-- Measured with DuckDB 1.5.2 and Quack core_nightly 893744a (2026-10-25); scripts/authz-check.sh proves it.
CREATE OR REPLACE MACRO read_only(sid, query) AS (
    CASE WHEN coalesce(NOT (json_serialize_sql(query::VARCHAR)->>'error')::BOOLEAN, false)
          AND json_array_length(json_serialize_sql(query::VARCHAR)->'statements') = 1
          AND NOT regexp_matches(json_serialize_sql(query::VARCHAR)::VARCHAR,
                '"function_name":"(read_[a-z_]*|[a-z_]*_scan|glob|sniff_csv|parquet_[a-z_]*|duckdb_secrets|duckdb_settings|which_secret|getenv)"|"table_name":"[^"]*[/\\.:]')
         THEN query END
);
SET GLOBAL quack_authorization_function = 'read_only';
