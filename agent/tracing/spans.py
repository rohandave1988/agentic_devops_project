"""Span helpers used throughout the agent codebase.

Usage:
    from tracing.spans import agent_span

    with agent_span("specialist.metrics", **{"incident.id": id, "question": q}) as sp:
        result = do_work()
        sp.set_attribute("result.confidence", 0.87)

Spans record exceptions automatically and set ERROR status if one propagates.
If no tracer provider is configured, OTel uses a NoOp tracer — safe to use
before setup_tracing() is called (e.g. in tests).
"""
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode


@contextmanager
def agent_span(name: str, **attrs: Any):
    """Create a named OTel span as a context manager.

    Sets span attributes from kwargs, records any exception as a span event,
    and marks the span ERROR on propagating exception or OK on clean exit.
    """
    tracer = trace.get_tracer("devops-agent")
    with tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                try:
                    sp.set_attribute(k, v)
                except Exception:
                    sp.set_attribute(k, str(v))
        try:
            yield sp
        except Exception as exc:
            sp.record_exception(exc)
            sp.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        else:
            sp.set_status(Status(StatusCode.OK))


def set_span_attrs(sp: Span, **attrs: Any) -> None:
    """Set multiple attributes on a span, skipping None values."""
    for k, v in attrs.items():
        if v is not None:
            try:
                sp.set_attribute(k, v)
            except Exception:
                sp.set_attribute(k, str(v))


def current_trace_id() -> str:
    """Return the active span's trace ID as a 32-char hex string, or zeros."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else "0" * 32


def current_span_id() -> str:
    """Return the active span's span ID as a 16-char hex string, or zeros."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.span_id, "016x") if ctx.is_valid else "0" * 16
