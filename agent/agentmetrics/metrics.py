"""Agent self-observability — mirrors the Go agent's Prometheus metrics exactly."""
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

logger = logging.getLogger(__name__)

CYCLES = Counter(
    "agent_cycles_total", "Total agent polling cycles executed."
)
SLO_CHECKS = Counter(
    "agent_slo_checks_total", "SLO check results per cycle.", ["result"]
)
INCIDENTS = Counter(
    "agent_incidents_total", "Incidents detected by severity.", ["severity"]
)
ACTIONS_EXECUTED = Counter(
    "agent_actions_executed_total", "Remediation actions dispatched.", ["action"]
)
ACTIONS_BLOCKED = Counter(
    "agent_actions_blocked_total", "Actions blocked by safety layer.", ["action", "reason"]
)
VERIFICATIONS = Counter(
    "agent_verifications_total", "Post-action verification results.", ["action", "result"]
)
MTTR = Histogram(
    "agent_mttr_seconds",
    "Mean time to recovery in seconds.",
    buckets=[30, 60, 90, 120, 180, 300, 600],
)
LLM_CALLS = Counter(
    "agent_llm_calls_total", "LLM API calls by backend and result.", ["backend", "result"]
)
LLM_LATENCY = Histogram(
    "agent_llm_latency_seconds",
    "LLM API call duration in seconds.",
    ["backend"],
    buckets=[1, 2, 5, 10, 20, 30, 60, 120],
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/metrics":
            data = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_):  # suppress noisy access log
        pass


def start_server(port: int) -> None:
    server = HTTPServer(("", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"agent metrics server listening on :{port}")
