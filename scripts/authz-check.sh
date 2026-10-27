#!/usr/bin/env bash
# authz-check.sh: proves the demo's read-only posture against the RUNNING core stack (make up / make smoke).
#
#   1. the data server (quack-server) refuses writes, multi-statements and file reads even from a client that holds
#      its token, while reads and ATTACH work (the authorization callback, quack-server/authz.sql);
#   2. the token is not in the data server's log;
#   3. the agents' door (edge, :5556) offers the toolsets' MCP endpoints only: not /mcp (every tool), not
#      /api/tool/<name>/invoke, not /ui, not the analytics_dev toolset;
#   4. the Toolbox's validator refuses destructive and file-reading SQL on the dev tool (person's door, :5555).
#
# Exits non-zero on the first table with a failure. Needs docker compose, curl and the .env of the stack.
set -uo pipefail
cd "$(dirname "$0")/.."
COMPOSE=${COMPOSE:-docker compose}
EDGE=${EDGE_URL:-http://localhost:5556}
TOOLBOX=${TOOLBOX_HOST_URL:-http://localhost:5555}
fail=0
ok() { printf '  ok    %s\n' "$1"; }
bad() { printf '  FAIL  %s\n' "$1"; fail=1; }

echo "1. data server: a client with the sales token, in the stack's network"
quack() { # quack SQL-for-quack_query: one row or the error, from a duckdb client in the quack-server image
	local q=${1//\'/\'\'}
	$COMPOSE run --rm --no-deps -T --entrypoint sh quack-server -c \
		"duckdb -csv -noheader -c \"LOAD quack; CREATE SECRET s (TYPE quack, TOKEN '\$QUACK_TOKEN'); SELECT * FROM quack_query('quack:quack-server:9494', '$q', disable_ssl := true);\" 2>&1 | grep -v '^true\$' | head -1" 2>/dev/null
}
attach=$($COMPOSE run --rm --no-deps -T --entrypoint sh quack-server -c \
	"duckdb -csv -noheader -c \"LOAD quack; CREATE SECRET s (TYPE quack, TOKEN '\$QUACK_TOKEN'); ATTACH 'quack:quack-server:9494' AS r (TYPE quack, DISABLE_SSL true); SELECT count(*) FROM r.sales;\" 2>&1 | grep -v '^true\$' | head -1" 2>/dev/null)
[ "$attach" = 15 ] && ok "ATTACH + SELECT count(*) FROM sales -> 15" || bad "ATTACH + SELECT -> $attach"
r=$(quack "SELECT count(*) FROM sales"); [ "$r" = 15 ] && ok "quack_query SELECT -> 15" || bad "quack_query SELECT -> $r"
for q in "DELETE FROM sales" "UPDATE sales SET amount = 0" "INSERT INTO sales VALUES (99, 'x', 1, DATE '2026-01-01')" \
	"DROP TABLE orders" "CREATE TABLE x AS SELECT 1" "SELECT 1; DELETE FROM sales" "COPY sales TO '/tmp/x.csv'" \
	"SELECT count(*) FROM read_csv('/etc/passwd')" "SELECT count(*) FROM '/etc/passwd'" "SELECT getenv('HOME')"; do
	r=$(quack "$q")
	case "$r" in *"Authorization failed"*) ok "refused: $q" ;; *) bad "NOT refused: $q -> $r" ;; esac
done
r=$(quack "SELECT count(*) FROM sales"); [ "$r" = 15 ] && ok "still 15 rows" || bad "rows after the refusals: $r"

echo "2. the token is not in the data server's log"
tok=$(grep -E '^QUACK_TOKEN_SALES=' .env | cut -d= -f2-)
if [ -n "$tok" ] && $COMPOSE logs quack-server 2>&1 | grep -qF -- "$tok"; then bad "QUACK_TOKEN_SALES appears in the quack-server log"; else ok "QUACK_TOKEN_SALES not in the quack-server log"; fi

echo "3. the agents' door ($EDGE)"
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"authz-check","version":"0"}}}'
for path in /mcp /mcp/ /mcp/analytics_dev /api/toolset /api/tool/dev_duckdb_execute_sql/invoke /ui/ /; do
	c=$(code -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$init" "$EDGE$path")
	[ "$c" = 404 ] && ok "$path -> 404" || bad "$path -> $c (want 404)"
done
c=$(code -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$init" "$EDGE/mcp/analytics_readonly")
[ "$c" = 200 ] && ok "/mcp/analytics_readonly initialize -> 200" || bad "/mcp/analytics_readonly -> $c (want 200)"

echo "4. the Toolbox's validator on the dev tool ($TOOLBOX, the person's door)"
call() { # call SQL: the tool result's text
	local body
	body=$(python3 -c 'import json,sys; print(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"dev_duckdb_execute_sql","arguments":{"sql":sys.argv[1]}}}))' "$1")
	curl -s -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$body" "$TOOLBOX/mcp/analytics_dev"
}
for q in "DELETE FROM remote.sales" "SELECT 1; DELETE FROM remote.sales" "SELECT count(*) FROM read_csv('/etc/passwd')" \
	"SELECT * FROM quack_query('quack:quack-server:9494', 'DELETE FROM sales', disable_ssl := true)"; do
	r=$(call "$q")
	case "$r" in *"rejected by policy"*) ok "refused: $q" ;; *) bad "NOT refused: $q -> ${r:0:160}" ;; esac
done

[ "$fail" = 0 ] && echo "authz-check: PASS" || echo "authz-check: FAIL"
exit "$fail"
