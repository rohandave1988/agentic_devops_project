"""Loki tools for the LogsAgent.

Tools query the Loki log store directly. The LogsAgent decides how far back
to look and which patterns to surface based on the violation context.
"""
import re
from collections import Counter

from langchain_core.tools import tool
from perception.loki import LokiClient

_ERROR_RE  = re.compile(r"\b(error|exception|traceback|panic|fatal|oom|killed)\b", re.I)
_EXCEPT_RE = re.compile(r"([A-Z][a-zA-Z]+(?:Error|Exception|Panic))")
_SKIP_RE   = re.compile(r"^\s*(DEBUG|TRACE)\b")


def make_loki_tools(loki: LokiClient) -> list:
    """Return tools bound to a specific LokiClient instance."""

    @tool
    def search_error_logs(lookback_sec: int = 120) -> str:
        """Fetch recent error and warning log lines from the application.

        Args:
            lookback_sec: How many seconds of history to search (default 120).
        """
        lines  = loki.query_recent_logs(lookback_sec=lookback_sec)
        errors = [l for l in lines if _ERROR_RE.search(l) and not _SKIP_RE.match(l)]
        if not errors:
            return f"No error log lines found in the last {lookback_sec}s."
        sample = "\n".join(errors[-15:])
        return f"{len(errors)} error lines found. Last 15:\n{sample}"

    @tool
    def get_exception_summary(lookback_sec: int = 120) -> str:
        """Get exception types grouped by frequency from recent logs.

        Args:
            lookback_sec: How many seconds of history to search (default 120).
        """
        lines  = loki.query_recent_logs(lookback_sec=lookback_sec)
        counts: Counter = Counter()
        for line in lines:
            for exc in _EXCEPT_RE.findall(line):
                counts[exc] += 1
        if not counts:
            return f"No recognisable exception types found in the last {lookback_sec}s."
        rows = "\n".join(f"  {exc}: {n}" for exc, n in counts.most_common(10))
        return f"Exception counts (top 10):\n{rows}"

    return [search_error_logs, get_exception_summary]
