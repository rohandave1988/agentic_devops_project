"""Agent self-observability — Prometheus metrics for the self-healing agent."""
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

logger = logging.getLogger(__name__)

# ── Cycle / SLO ───────────────────────────────────────────────────────────────
CYCLES = Counter(
    "agent_cycles_total", "Total agent polling cycles executed."
)
SLO_CHECKS = Counter(
    "agent_slo_checks_total", "SLO check results per cycle.", ["result"]
)
INCIDENTS = Counter(
    "agent_incidents_total", "Incidents detected by severity.", ["severity"]
)

# ── Actions ───────────────────────────────────────────────────────────────────
ACTIONS_EXECUTED = Counter(
    "agent_actions_executed_total", "Remediation actions dispatched.", ["action"]
)
ACTIONS_BLOCKED = Counter(
    "agent_actions_blocked_total", "Actions blocked by safety layer.", ["action", "reason"]
)

# ── Verification / MTTR ───────────────────────────────────────────────────────
VERIFICATIONS = Counter(
    "agent_verifications_total", "Post-action verification results.", ["action", "result"]
)
MTTR = Histogram(
    "agent_mttr_seconds",
    "Mean time to recovery in seconds.",
    buckets=[30, 60, 90, 120, 180, 300, 600],
)

# ── LLM ───────────────────────────────────────────────────────────────────────
LLM_CALLS = Counter(
    "agent_llm_calls_total", "LLM API calls by backend and result.", ["backend", "result"]
)
LLM_LATENCY = Histogram(
    "agent_llm_latency_seconds",
    "LLM API call duration in seconds.",
    ["backend"],
    buckets=[1, 2, 5, 10, 20, 30, 60, 120],
)

# ── Investigation quality (agentic reasoning signals) ─────────────────────────

INVESTIGATION_DURATION = Histogram(
    "agent_investigation_duration_seconds",
    "End-to-end time from SLO breach detection to final diagnosis.",
    buckets=[5, 10, 20, 30, 45, 60, 90, 120, 180],
)
"""How long the multi-agent investigation loop takes. Useful for comparing
Claude vs Ollama response times and tuning MAX_ITER limits."""

SPECIALIST_CALLS = Counter(
    "agent_specialist_calls_total",
    "Calls dispatched to each specialist agent per investigation.",
    ["specialist"],
)
"""Tracks which specialists (metrics/logs/history) get used most often.
High logs-agent call rate suggests metrics alone aren't sufficient."""

DIAGNOSIS_CONFIDENCE = Histogram(
    "agent_diagnosis_confidence",
    "LLM confidence score at the time an action is selected (0.0–1.0).",
    buckets=[0.3, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0],
)
"""Distribution of confidence scores when the agent acts. A skew toward
low values means the agent is frequently acting on weak evidence."""

SELF_REFLECTION = Counter(
    "agent_self_reflection_total",
    "Outcomes of the self-reflection pass after initial diagnosis.",
    ["result"],  # "confirmed" | "revised"
)
"""How often the self-reflection pass changes the initial diagnosis.
High 'revised' rate = initial specialist findings are poor quality."""

HITL_DECISIONS = Counter(
    "agent_hitl_decisions_total",
    "Human-in-the-loop decisions at Gate 1 (action selection).",
    ["outcome"],  # "accepted" | "overridden" | "timeout"
)
"""Tracks operator trust in AI recommendations.
High 'overridden' rate = AI action suggestions don't match operator intuition."""

EVIDENCE_GATE_REJECTIONS = Counter(
    "agent_evidence_gate_rejections_total",
    "Times finalize_diagnosis was rejected for insufficient specialist evidence.",
)
"""How often the orchestrator tries to skip the evidence gate.
Non-zero = the LLM is attempting shortcuts; the gate is doing its job."""

THROUGHPUT = Histogram(
    "agent_cluster_rps",
    "Observed HTTP request rate (req/s) at time of SLO check.",
    buckets=[0.5, 1, 2, 5, 10, 20, 50, 100, 200],
)
"""Request throughput at the time of each SLO evaluation. Helps distinguish
'1 bad request = 100% error rate' (low RPS) from genuine load problems."""


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
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"agent metrics server listening on :{port}")
