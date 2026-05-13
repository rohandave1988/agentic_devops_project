"""Prometheus tools for the MetricsAgent.

All tools operate on a pre-fetched ClusterMetrics snapshot — no extra
network calls. The MetricsAgent decides which to call and in what order.
"""
import config
from langchain_core.tools import tool
from perception.prometheus import ClusterMetrics


def make_prometheus_tools(metrics: ClusterMetrics) -> list:
    """Return tools bound to a specific ClusterMetrics snapshot."""

    @tool
    def get_error_rates() -> str:
        """Get current HTTP 5xx and 4xx error rates compared to SLO thresholds."""
        return (
            f"5xx error rate : {metrics.error_rate * 100:.2f}%"
            f"  (SLO < {config.SLO_ERROR_RATE * 100:.0f}%,"
            f"  {'BREACH' if metrics.error_rate > config.SLO_ERROR_RATE else 'OK'})\n"
            f"4xx error rate : {metrics.http_4xx_rate * 100:.2f}%"
            f"  (SLO < {config.SLO_4XX_RATE * 100:.0f}%,"
            f"  {'BREACH' if metrics.http_4xx_rate > config.SLO_4XX_RATE else 'OK'})"
        )

    @tool
    def get_latency_stats() -> str:
        """Get P99 and P50 request latency compared to SLO thresholds."""
        return (
            f"P99 latency : {metrics.latency_p99_ms:.0f} ms"
            f"  (SLO < {config.SLO_LATENCY_MS:.0f} ms,"
            f"  {'BREACH' if metrics.latency_p99_ms > config.SLO_LATENCY_MS else 'OK'})\n"
            f"P50 latency : {metrics.latency_p50_ms:.0f} ms"
            f"  (SLO < {config.SLO_LATENCY_P50:.0f} ms,"
            f"  {'BREACH' if metrics.latency_p50_ms > config.SLO_LATENCY_P50 else 'OK'})"
        )

    @tool
    def get_resource_usage() -> str:
        """Get CPU usage, CPU throttle ratio, and memory usage vs SLO."""
        return (
            f"CPU usage    : {metrics.cpu_usage * 100:.1f}%"
            f"  (SLO < {config.SLO_CPU * 100:.0f}%,"
            f"  {'BREACH' if metrics.cpu_usage > config.SLO_CPU else 'OK'})\n"
            f"CPU throttle : {metrics.cpu_throttle_ratio * 100:.1f}%"
            f"  (SLO < {config.SLO_CPU_THROTTLE * 100:.0f}%,"
            f"  {'BREACH' if metrics.cpu_throttle_ratio > config.SLO_CPU_THROTTLE else 'OK'})\n"
            f"Memory       : {metrics.memory_usage * 100:.1f}%"
            f"  (SLO < {config.SLO_MEMORY * 100:.0f}%,"
            f"  {'BREACH' if metrics.memory_usage > config.SLO_MEMORY else 'OK'})"
        )

    @tool
    def get_pod_health() -> str:
        """Get pod restart count, OOM kills, in-flight requests, and replica status."""
        return (
            f"Pod restarts   : {metrics.pod_restarts} in last 5 min\n"
            f"OOM kills      : {metrics.oom_kills} in last 5 min\n"
            f"Active requests: {metrics.active_requests}"
            f"  (SLO ≤ {config.SLO_ACTIVE_REQUESTS},"
            f"  {'BREACH' if metrics.active_requests > config.SLO_ACTIVE_REQUESTS else 'OK'})\n"
            f"Replicas       : {metrics.ready_replicas}/{metrics.desired_replicas} ready"
        )

    return [get_error_rates, get_latency_stats, get_resource_usage, get_pod_health]
