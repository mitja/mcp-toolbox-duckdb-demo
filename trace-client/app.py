"""Minimal OTel-instrumented Toolbox client.

Speaks MCP JSON-RPC at /mcp because Toolbox extracts incoming W3C
TraceContext from the MCP `_meta.traceparent` field (see
internal/server/mcp.go in the fork), NOT from the HTTP `traceparent`
header on the REST `/api/tool/.../invoke` endpoint. Hitting /api/...
with the auto-injected HTTP header alone produces a disjoint
toolbox-side trace; routing through MCP with traceparent in `_meta`
stitches the trace across services.

Open http://localhost:16686 (Jaeger), pick service `trace-client`,
and the full hierarchy renders:

    client.invoke                          (trace-client)
      POST /mcp                            (trace-client, auto-instrumented)
        toolbox/server/mcp/http            (toolbox-duckdb)
          toolbox/server/tool/invoke       (toolbox-duckdb)
            duckdb.query                   (duckdbquack source)
              -> db.system, source.name, response.rows, ...

Reads OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_SERVICE_NAME from the env;
defaults work inside the demo's docker network.
"""
from __future__ import annotations

import json
import os
import sys

import requests
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup_tracing() -> trace.Tracer:
    service = os.environ.get("OTEL_SERVICE_NAME", "trace-client")
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    # Auto-instrumentation creates a child span for the HTTP call and
    # injects an HTTP `traceparent` header. The HTTP header is redundant
    # for the toolbox-side stitch (Toolbox reads `_meta.traceparent` from
    # the JSON-RPC body, not from headers) but the auto-span is useful
    # context in Jaeger.
    RequestsInstrumentor().instrument()
    return trace.get_tracer(__name__)


def main() -> int:
    tracer = setup_tracing()
    toolbox = os.environ.get("TOOLBOX_URL", "http://toolbox:5000")
    pattern = os.environ.get("CUSTOMER_PATTERN", "gmbh")

    print(f"toolbox: {toolbox}")
    print(f"pattern: {pattern!r}")

    with tracer.start_as_current_span("client.invoke") as span:
        span.set_attribute("toolbox.tool.name", "revenue_by_customer")

        # Build the W3C traceparent string from the current span context
        # and embed it in MCP's `_meta` field. int(sc.trace_flags) so the
        # IntFlag formats cleanly as %02x across OTel SDK versions.
        sc = span.get_span_context()
        traceparent = (
            f"00-{sc.trace_id:032x}-{sc.span_id:016x}-"
            f"{int(sc.trace_flags):02x}"
        )

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "revenue_by_customer",
                "arguments": {"customer_pattern": pattern},
                "_meta": {"traceparent": traceparent},
            },
        }

        resp = requests.post(
            f"{toolbox}/mcp",
            json=payload,
            headers={"Accept": "application/json, text/event-stream"},
            timeout=30,
        )
        resp.raise_for_status()
        envelope = resp.json()

        # MCP `tools/call` response shape: result.content is a list of
        # content blocks; for our duckdb-sql tool, [0].text holds the
        # spec §7 JSON envelope our Source.RunSQL emits.
        text = envelope["result"]["content"][0]["text"]
        body = json.loads(text)

        span.set_attribute("toolbox.response.row_count", body.get("row_count", 0))
        print(f"\ntraceparent (sent in _meta): {traceparent}")
        print(f"row_count = {body['row_count']}")
        print(f"trace_id  = {format(sc.trace_id, '032x')}")
        print(f"\ncolumns: {[c['name'] for c in body['columns']]}")
        for row in body["rows"]:
            print(f"  {row}")

    # Force flush before the short-lived container exits.
    trace.get_tracer_provider().force_flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
