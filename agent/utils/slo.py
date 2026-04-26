import config
from perception.prometheus import ClusterMetrics


def check_slos(metrics: ClusterMetrics) -> list[str]:
    """Return a list of human-readable SLO violation strings. Empty = healthy."""
    violations: list[str] = []

    if metrics.error_rate > config.SLO_ERROR_RATE:
        violations.append(
            f"ERROR_RATE_BREACH: {metrics.error_rate*100:.2f}% > {config.SLO_ERROR_RATE*100:.2f}% SLO"
        )
    if metrics.latency_p99_ms > config.SLO_LATENCY_MS:
        violations.append(
            f"LATENCY_BREACH: {metrics.latency_p99_ms:.0f}ms > {config.SLO_LATENCY_MS:.0f}ms SLO"
        )
    if metrics.cpu_usage > config.SLO_CPU:
        violations.append(
            f"CPU_BREACH: {metrics.cpu_usage*100:.2f}% > {config.SLO_CPU*100:.2f}% SLO"
        )
    if metrics.memory_usage > config.SLO_MEMORY:
        violations.append(
            f"MEMORY_BREACH: {metrics.memory_usage*100:.2f}% > {config.SLO_MEMORY*100:.2f}% SLO"
        )
    if metrics.pod_restarts > 3:
        violations.append(
            f"POD_CRASH_LOOP: {metrics.pod_restarts} restarts in last 5 min"
        )
    if metrics.ready_replicas < metrics.desired_replicas:
        deficit = metrics.desired_replicas - metrics.ready_replicas
        violations.append(f"REPLICA_DEFICIT: {deficit} pod(s) not ready")

    return violations
