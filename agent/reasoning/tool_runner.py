import json
import logging

from perception.prometheus import ClusterMetrics
from perception.loki import LokiClient, format_for_llm

logger = logging.getLogger(__name__)


class ToolRunner:
    """Executes tool calls on behalf of the LLM.

    Holds the current cycle's metrics snapshot so get_metrics is served
    instantly without a second Prometheus round-trip. Loki and memory
    are only queried if the LLM actually requests them.
    """

    def __init__(self, metrics: ClusterMetrics, loki: LokiClient, store):
        self._metrics = metrics
        self._loki    = loki
        self._store   = store

    def execute(self, name: str, _input: dict) -> str:
        logger.info(f"tool executing: {name}")
        if name == "get_metrics":
            return self._get_metrics()
        if name == "get_recent_logs":
            return self._get_recent_logs()
        if name == "get_incident_history":
            return self._get_incident_history()
        return f"error: unknown tool {name!r}"

    def _get_metrics(self) -> str:
        m = self._metrics
        import config
        payload = {
            "current": {
                "error_rate_pct":   f"{m.error_rate * 100:.2f}",
                "latency_p99_ms":   f"{m.latency_p99_ms:.0f}",
                "cpu_usage_pct":    f"{m.cpu_usage * 100:.2f}",
                "memory_usage_pct": f"{m.memory_usage * 100:.2f}",
                "pod_restarts_5m":  m.pod_restarts,
                "ready_replicas":   m.ready_replicas,
                "desired_replicas": m.desired_replicas,
            },
            "slo_thresholds": {
                "error_rate_pct":   f"{config.SLO_ERROR_RATE * 100:.2f}",
                "latency_p99_ms":   f"{config.SLO_LATENCY_MS:.0f}",
                "cpu_usage_pct":    f"{config.SLO_CPU * 100:.2f}",
                "memory_usage_pct": f"{config.SLO_MEMORY * 100:.2f}",
                "max_pod_restarts": "3",
                "ready_must_equal": "desired_replicas",
            },
        }
        return json.dumps(payload, indent=2)

    def _get_recent_logs(self) -> str:
        lines = self._loki.query_recent_logs(120)
        return format_for_llm(lines)

    def _get_incident_history(self) -> str:
        history = self._store.get_recent(5)
        if not history:
            return "No previous incidents recorded."
        return json.dumps(history, indent=2)
