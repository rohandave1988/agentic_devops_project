"""Shared utilities used inside every specialist agent's tool loop."""
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from langchain_core.messages import AIMessage, ToolMessage

_tracer = trace.get_tracer("devops-agent")


def run_tool(tool_map: dict, tc: dict) -> str:
    """Execute one tool call and return its string result.

    Each call is wrapped in an OTel span so every tool invocation appears as
    a child span under the enclosing specialist span in Jaeger.
    """
    name    = tc["name"]
    args    = tc.get("args", {})
    tool_fn = tool_map.get(name)

    if tool_fn is None:
        return f"Unknown tool: {name}"

    with _tracer.start_as_current_span(f"tool.{name}") as sp:
        sp.set_attribute("tool.name", name)
        try:
            result = str(tool_fn.invoke(args))
            sp.set_status(Status(StatusCode.OK))
            return result
        except Exception as exc:
            sp.record_exception(exc)
            sp.set_status(Status(StatusCode.ERROR, str(exc)))
            return f"Tool '{name}' error: {exc}"


def extract_text(messages: list) -> str:
    """Return the last non-empty AI message text — the agent's final analysis."""
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        content = msg.content
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        return text
    return ""
