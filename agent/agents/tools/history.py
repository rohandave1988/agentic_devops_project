"""History tools for the HistoryAgent.

Tools query the SQLite incident store. The HistoryAgent uses these to
identify repeated failures, action success rates, and metric trend direction.
"""
from collections import Counter

from langchain_core.tools import tool


def make_history_tools(store) -> list:
    """Return tools bound to a specific Store instance."""

    @tool
    def get_recent_incidents(n: int = 5) -> str:
        """Retrieve the N most recent incidents with their actions and outcomes.

        Args:
            n: Number of recent incidents to retrieve (default 5).
        """
        incidents = store.get_recent(n=n)
        if not incidents:
            return "No incident history found."
        rows = []
        for inc in incidents:
            rows.append(
                f"  cause={inc.get('root_cause', '?')!r}"
                f"  severity={inc.get('severity', '?')}"
                f"  action={inc.get('action_taken', 'none')}"
                f"  recovered={inc.get('slo_recovered', False)}"
            )
        return f"Last {len(incidents)} incidents:\n" + "\n".join(rows)

    @tool
    def get_action_outcomes() -> str:
        """Get success and failure counts for each remediation action tried historically."""
        incidents = store.get_recent(n=20)
        outcomes: dict[str, dict] = {}
        for inc in incidents:
            action = inc.get("action_taken", "")
            if not action:
                continue
            if action not in outcomes:
                outcomes[action] = {"success": 0, "fail": 0}
            if inc.get("slo_recovered"):
                outcomes[action]["success"] += 1
            else:
                outcomes[action]["fail"] += 1
        if not outcomes:
            return "No action history."
        rows = []
        for action, counts in outcomes.items():
            total = counts["success"] + counts["fail"]
            pct   = counts["success"] / total * 100 if total else 0
            rows.append(f"  {action}: {counts['success']}/{total} resolved ({pct:.0f}%)")
        return "Action outcomes:\n" + "\n".join(rows)

    @tool
    def get_metric_trend(window_minutes: int = 30) -> str:
        """Get metric trend deltas over a time window to detect worsening conditions.

        Args:
            window_minutes: Lookback window in minutes (default 30).
        """
        points = store.get_metric_trend(minutes=window_minutes)
        if len(points) < 2:
            return "Insufficient metric history for trend analysis."
        first, last = points[0], points[-1]
        window = round((last["ts"] - first["ts"]) / 60, 1)
        return (
            f"Trend over {window} min:\n"
            f"  CPU     : {(last['cpu_usage']    - first['cpu_usage'])    * 100:+.1f}%\n"
            f"  Memory  : {(last['memory_usage'] - first['memory_usage']) * 100:+.1f}%\n"
            f"  5xx err : {(last['error_rate']   - first['error_rate'])   * 100:+.2f}%\n"
            f"  Latency : {last['latency_p99_ms'] - first['latency_p99_ms']:+.0f} ms"
        )

    return [get_recent_incidents, get_action_outcomes, get_metric_trend]
