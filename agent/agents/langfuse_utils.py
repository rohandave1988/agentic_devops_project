"""Conditional Langfuse tracing — safe no-ops when LANGFUSE_ENABLED=false.

Usage in any agent:
    from agents.langfuse_utils import observe, langfuse_context

    class MyAgent:
        @observe(name="my-agent")
        def run(self, ctx, question):
            langfuse_context.update_current_observation(
                input={"question": question},
            )
            ...
            langfuse_context.update_current_observation(
                output={"analysis": result.analysis},
            )
            return result
"""
import logging
import config

_log = logging.getLogger(__name__)

ENABLED = False


def _noop_decorator(fn=None, *, name=None, capture_input=True, capture_output=True, **kwargs):
    """No-op stand-in for @observe when Langfuse is disabled."""
    if fn is not None:
        return fn
    def wrapper(f):
        return f
    return wrapper


class _NoopContext:
    def update_current_trace(self, **kwargs):       pass
    def update_current_observation(self, **kwargs): pass


observe          = _noop_decorator
langfuse_context = _NoopContext()

if config.LANGFUSE_ENABLED:
    try:
        from langfuse.decorators import (
            observe          as _observe,
            langfuse_context as _langfuse_context,
        )
        observe          = _observe
        langfuse_context = _langfuse_context
        ENABLED          = True
        _log.info(
            "Langfuse tracing enabled",
            extra={"host": config.LANGFUSE_HOST},
        )
    except Exception as exc:
        _log.warning(f"Langfuse import failed — tracing disabled: {exc}")
