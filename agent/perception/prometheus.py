from dataclasses import dataclass
import logging

import requests

import config

logger = logging.getLogger(__name__)


@dataclass
class ClusterMetrics:
    error_rate: float = 0.0
    latency_p99_ms: float = 0.0
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    pod_restarts: int = 0
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

        return ClusterMetrics(
            error_rate=qf(
                # cluster mode
                f'sum(rate(http_requests_total{{namespace="{ns}",status_code=~"5.."}}[2m]))'
                f' / sum(rate(http_requests_total{{namespace="{ns}"}}[2m]))',
                # local mode — buggy-app gauge
                'app_error_rate',
            ),
            latency_p99_ms=qf(
                f'histogram_quantile(0.99, sum(rate(http_request_duration_ms_bucket{{namespace="{ns}"}}[2m])) by (le))',
                'histogram_quantile(0.99, sum(rate(http_request_duration_ms_bucket[2m])) by (le))',
            ),
            cpu_usage=qf(
                # cluster mode — ratio vs limits
                f'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}",container="{dep}"}}[2m]))'
                f' / sum(kube_pod_container_resource_limits{{namespace="{ns}",container="{dep}",resource="cpu"}})',
                # local mode — psutil percent → 0-1 ratio
                'app_cpu_usage_percent / 100',
            ),
            memory_usage=qf(
                f'sum(container_memory_working_set_bytes{{namespace="{ns}",container="{dep}"}})'
                f' / sum(kube_pod_container_resource_limits{{namespace="{ns}",container="{dep}",resource="memory"}})',
                'app_memory_usage_percent / 100',
            ),
            pod_restarts=int(qf(
                f'sum(increase(kube_pod_container_status_restarts_total{{namespace="{ns}"}}[5m]))',
            )),
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
