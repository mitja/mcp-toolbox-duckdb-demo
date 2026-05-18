"""Concurrent multi-source test for the MCP Toolbox DuckDB/Quack adapter.

Fires N parallel MCP `tools/call` requests fanned out across both Quack
sources (sales-quack and inventory-quack), each request generating its
own root span and embedding W3C `traceparent` in MCP `_meta` for
end-to-end propagation.

After the burst, polls the Jaeger HTTP API to verify:

  1. Every request returned valid (non-error) JSON.
  2. Every emitted trace ID is findable in Jaeger (no dropped exports).
  3. Each trace contains a `duckdb.query` span that is a descendant of
     the client's root span — i.e. traceparent propagation survived
     concurrency.

Then prints throughput and latency stats per tool. Exits non-zero on
any failure so the script doubles as a CI smoke test.

Env (defaults match the demo's docker network):
    TOOLBOX_URL                 http://toolbox:5000
    JAEGER_URL                  http://jaeger:16686
    N_CONCURRENT                20
    OTEL_EXPORTER_OTLP_ENDPOINT http://otel-collector:4318
    OTEL_SERVICE_NAME           trace-load
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import random
import statistics
import sys
import time

import requests
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


# Fan out across all three sources. Each entry is (tool_name,
# kwargs_factory) — the factory returns a fresh dict per call so we
# vary parameters without sharing mutable state.
#
# Mix:
#   - 2 single-source sales tools (push down to quack-server),
#   - 2 single-source inventory tools (push down to quack-server-2),
#   - 1 parquet-backed inventory tool: hits the read_parquet view
#     exposed by the remote DuckDB on quack-server-2; same physical
#     path as the inventory tools but the rows originate from a
#     parquet file mounted next to the remote.
#   - 1 multi-attach tool (combined-analytics): joins inventory.products
#     with sales.orders in one query, executed locally by the Toolbox-
#     side DuckDB after rows stream from both remotes. Includes it
#     deliberately so the latency stats and Jaeger spans show the
#     local-execution path next to the pushdown path.
CUSTOMER_PATTERNS = ["gmbh", "corp", "ag", "sarl", "ltd"]
PRODUCT_PATTERNS  = ["Widget", "Bearing", "Bolt", "Cable", "Soldering"]
TASKS = [
    ("revenue_by_customer",      lambda: {"customer_pattern": random.choice(CUSTOMER_PATTERNS)}),
    ("top_products",             lambda: {}),
    ("low_stock_items",          lambda: {}),
    ("inventory_summary",        lambda: {}),
    ("price_history_for_product",lambda: {"product_pattern":  random.choice(PRODUCT_PATTERNS)}),
    ("product_orders_overview",  lambda: {}),
]


@dataclasses.dataclass
class Result:
    tool: str
    trace_id: str
    span_id: str
    ok: bool
    error: str | None
    rows: int
    duration_s: float


def setup_tracing() -> trace.Tracer:
    service = os.environ.get("OTEL_SERVICE_NAME", "trace-load")
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    RequestsInstrumentor().instrument()
    return trace.get_tracer(__name__)


def invoke(tracer: trace.Tracer, toolbox: str, tool: str, args: dict, req_id: int) -> Result:
    t0 = time.monotonic()
    # Each thread starts its own root span. start_as_current_span uses
    # contextvars (per-thread), so the trace IDs stay disjoint even
    # though the same tracer is shared across threads.
    with tracer.start_as_current_span(f"client.invoke.{tool}") as span:
        span.set_attribute("toolbox.tool.name", tool)
        sc = span.get_span_context()
        trace_id = f"{sc.trace_id:032x}"
        span_id = f"{sc.span_id:016x}"
        traceparent = f"00-{trace_id}-{span_id}-{int(sc.trace_flags):02x}"

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {
                "name": tool,
                "arguments": args,
                "_meta": {"traceparent": traceparent},
            },
        }
        try:
            resp = requests.post(
                f"{toolbox}/mcp",
                json=payload,
                headers={"Accept": "application/json, text/event-stream"},
                timeout=60,
            )
            resp.raise_for_status()
            env = resp.json()
            if "error" in env:
                raise RuntimeError(env["error"])
            text = env["result"]["content"][0]["text"]
            body = json.loads(text)
            rows = body.get("row_count", 0)
            span.set_attribute("toolbox.response.row_count", rows)
            return Result(tool, trace_id, span_id, True, None, rows, time.monotonic() - t0)
        except Exception as e:
            span.set_attribute("error", True)
            return Result(tool, trace_id, span_id, False, str(e), 0, time.monotonic() - t0)


def run_burst(tracer: trace.Tracer, toolbox: str, n: int) -> list[Result]:
    print(f"firing {n} concurrent calls across {len(TASKS)} tools "
          f"(3 sources: sales-quack, inventory-quack [incl. parquet-backed view], "
          f"combined-analytics)...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        futs = []
        # Round-robin across tools so each tool gets ~n/4 calls.
        for i in range(n):
            tool, mkargs = TASKS[i % len(TASKS)]
            futs.append(pool.submit(invoke, tracer, toolbox, tool, mkargs(), i + 1))
        return [f.result() for f in concurrent.futures.as_completed(futs)]


def fetch_trace(jaeger: str, trace_id: str) -> dict | None:
    try:
        r = requests.get(f"{jaeger}/api/traces/{trace_id}", timeout=10)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    if not data.get("data"):
        return None
    return data["data"][0]


def verify_in_jaeger(jaeger: str, results: list[Result],
                     max_wait_s: float = 60.0) -> tuple[int, int, list[str]]:
    """Poll Jaeger for every OK request's trace.

    Returns (complete_count, stitched_count, missing_trace_ids).

    A trace counts as "complete" only once it contains a `duckdb.query`
    span — toolbox-side spans arrive after their own OTel batch flush
    (typically 5-15s behind client spans), so we keep re-fetching even
    after the trace ID first becomes resolvable.

    A trace counts as "stitched" when that `duckdb.query` span is a
    descendant of a `client.invoke*` span via CHILD_OF references.
    """
    targets = [r.trace_id for r in results if r.ok]
    start = time.monotonic()
    complete: dict[str, dict] = {}
    print(f"\npolling Jaeger for {len(targets)} traces (up to {max_wait_s:.0f}s; "
          f"toolbox spans arrive on their own batch flush)...")
    iter_count = 0
    while time.monotonic() - start < max_wait_s:
        for tid in targets:
            if tid in complete:
                continue
            tr = fetch_trace(jaeger, tid)
            if tr is None:
                continue
            ops = {s["operationName"] for s in tr["spans"]}
            if "duckdb.query" in ops:
                complete[tid] = tr
        if len(complete) == len(targets):
            break
        iter_count += 1
        if iter_count % 5 == 0:
            print(f"  ... {len(complete)}/{len(targets)} complete after "
                  f"{time.monotonic() - start:.0f}s")
        time.sleep(2)

    stitched = 0
    for tr in complete.values():
        spans_by_id = {s["spanID"]: s for s in tr["spans"]}
        for s in tr["spans"]:
            if s["operationName"] != "duckdb.query":
                continue
            cur = s
            for _ in range(10):
                refs = [r for r in cur.get("references", []) if r["refType"] == "CHILD_OF"]
                if not refs:
                    break
                parent = spans_by_id.get(refs[0]["spanID"])
                if parent is None:
                    break
                if parent["operationName"].startswith("client.invoke"):
                    stitched += 1
                    break
                cur = parent
            break  # one duckdb.query per trace is enough
    missing_ids = [t for t in targets if t not in complete]
    return len(complete), stitched, missing_ids


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def main() -> int:
    tracer = setup_tracing()

    toolbox = os.environ.get("TOOLBOX_URL", "http://toolbox:5000")
    jaeger = os.environ.get("JAEGER_URL", "http://jaeger:16686")
    n = int(os.environ.get("N_CONCURRENT", "20"))

    print(f"toolbox: {toolbox}")
    print(f"jaeger:  {jaeger}")
    print(f"N:       {n}")
    print()

    wall_start = time.monotonic()
    results = run_burst(tracer, toolbox, n)
    wall = time.monotonic() - wall_start

    # Flush client spans so they reach Jaeger before we poll.
    trace.get_tracer_provider().force_flush()

    n_ok = sum(1 for r in results if r.ok)
    n_err = len(results) - n_ok
    ok_durations = [r.duration_s for r in results if r.ok]
    by_tool: dict[str, list[Result]] = {}
    for r in results:
        by_tool.setdefault(r.tool, []).append(r)

    print(f"\nrequests:   {len(results)} sent, {n_ok} ok, {n_err} error")
    print(f"wall time:  {wall:.2f}s")
    print(f"throughput: {len(results) / wall:.1f} req/s")
    if ok_durations:
        print(f"latency:    avg {statistics.mean(ok_durations):.3f}s  "
              f"p50 {percentile(ok_durations, 0.50):.3f}s  "
              f"p95 {percentile(ok_durations, 0.95):.3f}s  "
              f"max {max(ok_durations):.3f}s")
    print("by tool:")
    for tool, rs in sorted(by_tool.items()):
        ok = sum(1 for r in rs if r.ok)
        row_counts = sorted({r.rows for r in rs if r.ok})
        print(f"  {tool:24s} {len(rs):3d} sent, {ok:3d} ok, row_count(s)={row_counts}")

    complete, stitched, missing = verify_in_jaeger(jaeger, results)
    target = n_ok
    print(f"\nJaeger lookup:")
    print(f"  complete traces:    {complete}/{target}    (contain duckdb.query)")
    print(f"  spans stitched:     {stitched}/{complete}    (duckdb.query under client.invoke*)")
    if missing:
        print(f"  missing trace ids:  {missing[:5]}{'...' if len(missing) > 5 else ''}")

    if n_err:
        print("\nerrors:")
        for r in results:
            if not r.ok:
                print(f"  {r.tool:24s} trace={r.trace_id}  {r.error}")

    all_ok = (n_err == 0 and complete == target and stitched == complete)
    print(f"\nresult: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
