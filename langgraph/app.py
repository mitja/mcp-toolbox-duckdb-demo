"""Minimal LangGraph ReAct demo that calls MCP Toolbox tools.

The agent loads the analytics_readonly toolset from a Toolbox server, then
asks Claude one question that should be answered using the
revenue_by_customer tool. Prints the final response and the intermediate
tool calls so the user can see the curated DuckDB/Quack tools in action.

Required env:
    TOOLBOX_URL          (default: http://toolbox:5000)
    ANTHROPIC_API_KEY    Claude API key

Run with:
    docker compose --profile agent run --rm langgraph
"""
from __future__ import annotations

import os
import sys

from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent
from toolbox_langchain import ToolboxClient


def main() -> int:
    toolbox_url = os.environ.get("TOOLBOX_URL", "http://toolbox:5000")
    question = (
        "Which customers' names match 'gmbh', and what is each one's total "
        "revenue? List them from highest to lowest."
    )

    print(f"toolbox: {toolbox_url}")
    print(f"question: {question}\n")

    client = ToolboxClient(toolbox_url)
    tools = client.load_toolset("analytics_readonly")
    print(f"loaded {len(tools)} tools: {[t.name for t in tools]}\n")

    model = ChatAnthropic(model="claude-sonnet-4-6", temperature=0)
    agent = create_react_agent(model, tools)

    final = agent.invoke({"messages": [("user", question)]})
    for msg in final["messages"]:
        role = type(msg).__name__
        content = getattr(msg, "content", msg)
        print(f"--- {role} ---")
        print(content)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
