from dataclasses import dataclass
import logging

import requests

import config

logger = logging.getLogger(__name__)


@dataclass
class ClusterMetrics:
    error_rate: float = 0.0       # 5xx rate
    http_4xx_rate: float = 0.0    # 4xx rate — bad config/auth, not server error
    latency_p99_ms: float = 0.0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0   # less noisy than P99 for trend detection
    cpu_usage: float = 0.0
    cpu_throttle_ratio: float = 0.0  # fraction of CPU time cgroup-throttled
    memory_usage: float = 0.0
    pod_restarts: int = 0
    oom_kills: int = 0
    active_requests: int = 0      # in-flight requests — saturation indicator
    requests_per_second: float = 0.0  # total throughput — context for error rates
    ready_replicas: int = 0
    desired_replicas: int = 0


class PrometheusClient:
    def __init__(self):
        self._base = config.PROMETHEUS_URL
        self._session = requests.Session()

    def collect_metrics(self) -> ClusterMetrics:
        ns  = config.TARGET_NAMESPACE
        dep = config.TARGET_DEPLOYMENT
        qf  = self._query_first

        w = config.RATE_WINDOW
        return ClusterMetrics(
            error_rate=qf(
                f'sum(rate(http_requests_total{{namespace="{ns}",status_code=~"5.."}}[{w}]))'
                f' / sum(rate(http_requests_total{{namespace="{ns}"}}[{w}]))',
                'app_error_rate',
            ),
            http_4xx_rate=qf(
                # 4xx: bad config/auth/routing — distinct cause from 5xx, rollback is right fix
                f'sum(rate(http_requests_total{{namespace="{ns}",status_code=~"4.."}}[{w}]))'
                f' / sum(rate(http_requests_total{{namespace="{ns}"}}[{w}]))',
                'app_4xx_rate',
            ),
            latency_p99_ms=qf(
                f'histogram_quantile(0.99, sum(rate(http_request_duration_ms_bucket{{namespace="{ns}"}}[{w}])) by (le))',
                f'histogram_quantile(0.99, sum(rate(http_request_duration_ms_bucket[{w}])) by (le))',
            ),
            latency_p95_ms=qf(
                f'histogram_quantile(0.95, sum(rate(http_request_duration_ms_bucket{{namespace="{ns}"}}[{w}])) by (le))',
                f'histogram_quantile(0.95, sum(rate(http_request_duration_ms_bucket[{w}])) by (le))',
            ),
            latency_p50_ms=qf(
                f'histogram_quantile(0.50, sum(rate(http_request_duration_ms_bucket{{namespace="{ns}"}}[{w}])) by (le))',
                f'histogram_quantile(0.50, sum(rate(http_request_duration_ms_bucket[{w}])) by (le))',
            ),
            cpu_usage=qf(
                f'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}",container="{dep}"}}[{w}]))'
                f' / sum(kube_pod_container_resource_limits{{namespace="{ns}",container="{dep}",resource="cpu"}})',
                'app_cpu_usage_percent / 100',
            ),
            cpu_throttle_ratio=qf(
                # fraction of CPU scheduling periods where cgroup was throttled
                # HPA never sees this — a pod at 40% avg CPU can be throttled 80% of the time if bursty
                f'sum(rate(container_cpu_cfs_throttled_seconds_total{{namespace="{ns}",container="{dep}"}}[{w}]))'
                f' / sum(rate(container_cpu_cfs_periods_total{{namespace="{ns}",container="{dep}"}}[{w}]))',
            ),
            memory_usage=qf(
                f'sum(container_memory_working_set_bytes{{namespace="{ns}",container="{dep}"}})'
                f' / sum(kube_pod_container_resource_limits{{namespace="{ns}",container="{dep}",resource="memory"}})',
                'app_memory_usage_percent / 100',
            ),
            pod_restarts=int(qf(
                f'sum(increase(kube_pod_container_status_restarts_total{{namespace="{ns}"}}[5m]))',
            )),
            oom_kills=int(qf(
                # gauge (not counter) — increase() is wrong here; direct sum gives count of OOM-killed containers
                f'sum(kube_pod_container_status_last_terminated_reason{{namespace="{ns}",reason="OOMKilled"}})',
            )),
            active_requests=int(qf(
                # in-flight requests — saturation: app queuing, not dying
                f'sum(app_active_requests{{namespace="{ns}"}})',
                'app_active_requests',
            )),
            requests_per_second=qf(
                # total throughput — essential context for interpreting error rates
                # e.g. 100% error rate at 0.1 RPS is noise; at 50 RPS it's a crisis
                f'sum(rate(http_requests_total{{namespace="{ns}"}}[{w}]))',
                'sum(rate(http_requests_total[{w}]))',
            ),
            ready_replicas=int(qf(
                f'kube_deployment_status_replicas_ready{{namespace="{ns}",deployment="{dep}"}}',
                '1',  # local mode — single process = 1 replica
            )),
            desired_replicas=int(qf(
                f'kube_deployment_spec_replicas{{namespace="{ns}",deployment="{dep}"}}',
                '1',
            )),
        )

    def _query_first(self, *promqls: str) -> float:
        """Try each PromQL in order, return the first non-zero result."""
        for promql in promqls:
            val = self._query(promql)
            if val != 0.0:
                return val
        return 0.0

    def _query(self, promql: str) -> float:
        try:
            resp = self._session.get(
                f"{self._base}/api/v1/query",
                params={"query": promql},
                timeout=10,
            )
            resp.raise_for_status()
            result = resp.json()["data"]["result"]
            if result:
                return float(result[0]["value"][1])
        except Exception as e:
            logger.debug(f"prometheus query failed: {e} — query: {promql[:80]}")
        return 0.0
