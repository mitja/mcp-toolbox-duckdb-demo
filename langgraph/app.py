"""LangGraph ReAct demo wired through MCP Toolbox.

The agent loads the ontology toolset (the business-meaning layer)
plus the analytics_readonly toolset, then asks Claude a question
whose words are deliberately NOT tool names: "best sellers" should
be resolved through the glossary (top_product = units shipped, NOT
revenue) before the agent routes to the top_products tool and cites
the definition it used — the resolve-then-route loop the ontology
track exists to demonstrate.
With OTEL_EXPORTER_OTLP_ENDPOINT set in the env (it is, inside the
demo's docker network), every outgoing HTTP call from this process
is auto-instrumented: spans for the Toolbox tool call and for the
Anthropic API call land in the same trace as the LangGraph agent
span, and via W3C traceparent propagation the same trace continues
into Toolbox -> duckdb-quack source -> SQL.

Required env:
    TOOLBOX_URL                 (default: http://toolbox:5000)
    ANTHROPIC_API_KEY           Claude API key

Optional env (auto-set inside the demo compose):
    OTEL_EXPORTER_OTLP_ENDPOINT For distributed tracing, e.g.
                                http://otel-collector:4318
    OTEL_SERVICE_NAME           Default: langgraph-demo

Run with:
    docker compose --profile agent run --rm langgraph
"""
from __future__ import annotations

import os
import sys


def setup_tracing() -> object | None:
    """Configure OTel if OTEL_EXPORTER_OTLP_ENDPOINT is set.

    Imports are local so this module loads cleanly even when the OTel
    deps are missing (rare — they're in pyproject.toml — but keeps the
    failure mode obvious if someone strips them out).
    """
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    service = os.environ.get("OTEL_SERVICE_NAME", "langgraph-demo")
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    # Auto-instrument the HTTP libraries any layer of this stack might
    # reach for: requests (toolbox-langchain sync path), httpx
    # (toolbox-core async path + langchain-anthropic), urllib3 (the
    # underlying transport for several of these).
    RequestsInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    URLLib3Instrumentor().instrument()

    return trace.get_tracer(__name__)


def main() -> int:
    tracer = setup_tracing()

    # Imports after setup_tracing so the instrumentation patches the
    # client classes before they're imported.
    from langchain.agents import create_agent
    from langchain_anthropic import ChatAnthropic
    from toolbox_core.protocol import Protocol
    from toolbox_langchain import ToolboxClient

    toolbox_url = os.environ.get("TOOLBOX_URL", "http://toolbox:5000")
    question = "What are our best sellers right now?"

    # The resolve-then-route loop: business meaning first, data second.
    # This is the same instruction a platform tenant's sandbox agent
    # would ship with (see the ontology section in the README).
    system_prompt = (
        "You are this business's data analyst. Business terms have "
        "reviewed definitions here: before answering, resolve the "
        "user's words with glossary_lookup / ontology_search, use "
        "ontology_bindings to pick the right data tool, and respect "
        "any caveats. In your answer, cite the glossary definition "
        "you applied. If a term has no definition or no implementing "
        "tool, say so instead of guessing."
    )

    print(f"toolbox: {toolbox_url}")
    print(f"question: {question}\n")

    # telemetry_enabled=True is what makes the toolbox-core MCP transport
    # inject `_meta.traceparent` into each tool call; Toolbox extracts
    # that field and attaches the parent context, stitching the LangGraph
    # -> Toolbox -> duckdb-quack spans into a single trace in Jaeger.
    # Protocol.MCP_LATEST pins the newest protocol version the SDK knows
    # about (2025-11-25) so the `mcp` lib does not log its
    # "newer version available" nag at WARNING.
    client = ToolboxClient(
        toolbox_url,
        protocol=Protocol.MCP_LATEST,
        telemetry_enabled=True,
    )
    tools = client.load_toolset("ontology") + client.load_toolset(
        "analytics_readonly"
    )
    print(f"loaded {len(tools)} tools: {[t.name for t in tools]}\n")

    model = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)
    agent = create_agent(model, tools, system_prompt=system_prompt)

    def run() -> dict:
        return agent.invoke({"messages": [("user", question)]})

    if tracer is not None:
        with tracer.start_as_current_span("agent.invoke") as span:
            final = run()
            span.set_attribute(
                "agent.message_count", len(final.get("messages", []))
            )
    else:
        final = run()

    for msg in final["messages"]:
        role = type(msg).__name__
        content = getattr(msg, "content", msg)
        print(f"--- {role} ---")
        print(content)
        print()

    # Flush so the BatchSpanProcessor exports before the short-lived
    # container exits (default delay is 5s, which is often longer than
    # the agent's own lifetime).
    if tracer is not None:
        from opentelemetry import trace

        trace.get_tracer_provider().force_flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
