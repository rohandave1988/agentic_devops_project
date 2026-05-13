import config
from perception.prometheus import ClusterMetrics


def check_slos(metrics: ClusterMetrics) -> list[str]:
    """Return human-readable SLO violation strings. Empty list = healthy."""
    violations: list[str] = []

    if metrics.error_rate > config.SLO_ERROR_RATE:
        violations.append(
            f"ERROR_RATE_BREACH: {metrics.error_rate*100:.2f}% 5xx > {config.SLO_ERROR_RATE*100:.2f}% SLO"
        )
    if metrics.http_4xx_rate > config.SLO_4XX_RATE:
        violations.append(
            f"HTTP_4XX_BREACH: {metrics.http_4xx_rate*100:.2f}% client errors > {config.SLO_4XX_RATE*100:.2f}% SLO"
            " — likely bad deploy/config, not load"
        )
    if metrics.latency_p99_ms > config.SLO_LATENCY_MS:
        violations.append(
            f"LATENCY_P99_BREACH: {metrics.latency_p99_ms:.0f}ms > {config.SLO_LATENCY_MS:.0f}ms SLO"
        )
    if config.SLO_CHECK_P50 and metrics.latency_p50_ms > config.SLO_LATENCY_P50:
        violations.append(
            f"LATENCY_P50_BREACH: {metrics.latency_p50_ms:.0f}ms > {config.SLO_LATENCY_P50:.0f}ms SLO"
        )
    if metrics.cpu_usage > config.SLO_CPU:
        violations.append(
            f"CPU_BREACH: {metrics.cpu_usage*100:.2f}% > {config.SLO_CPU*100:.2f}% SLO"
        )
    if metrics.cpu_throttle_ratio > config.SLO_CPU_THROTTLE:
        violations.append(
            f"CPU_THROTTLE_BREACH: {metrics.cpu_throttle_ratio*100:.1f}% of CPU time throttled"
            f" > {config.SLO_CPU_THROTTLE*100:.0f}% SLO — HPA misses this, workload is bursty"
        )
    if metrics.memory_usage > config.SLO_MEMORY:
        violations.append(
            f"MEMORY_BREACH: {metrics.memory_usage*100:.2f}% > {config.SLO_MEMORY*100:.2f}% SLO"
        )
    if metrics.oom_kills > config.SLO_OOM_KILLS:
        violations.append(
            f"OOM_KILL: {metrics.oom_kills} container(s) OOM-killed in last 5 min — memory limit exceeded"
        )
    if metrics.active_requests > config.SLO_ACTIVE_REQUESTS:
        violations.append(
            f"REQUEST_SATURATION: {metrics.active_requests} active requests"
            f" > {config.SLO_ACTIVE_REQUESTS} SLO — app queuing, scale out"
        )
    if metrics.pod_restarts > 3:
        violations.append(
            f"POD_CRASH_LOOP: {metrics.pod_restarts} restarts in last 5 min"
        )
    if metrics.ready_replicas < metrics.desired_replicas:
        deficit = metrics.desired_replicas - metrics.ready_replicas
        violations.append(f"REPLICA_DEFICIT: {deficit} pod(s) not ready")

    return violations
