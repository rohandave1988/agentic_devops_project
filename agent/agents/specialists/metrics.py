"""MetricsAgent — Prometheus specialist."""
from agents.base import IncidentContext
from agents.specialists._base import SpecialistAgent
from agents.tools.prometheus import make_prometheus_tools
from agents.langfuse_utils import observe

_SYSTEM = """\
You are a Prometheus metrics specialist agent for a production Kubernetes service.
You have persistent memory of past investigations — use it to recognise patterns faster.

Your domain tools:
  get_error_rates    — HTTP 5xx and 4xx rates vs SLO thresholds
  get_latency_stats  — P50 and P99 latency vs SLO thresholds
  get_resource_usage — CPU usage, CPU throttle ratio, memory usage vs SLO
  get_pod_health     — pod restarts, OOM kills, in-flight requests, replica status

Investigation rules:
1. Use only the tools relevant to answering the question — not all of them blindly.
2. While investigating, note anything anomalous even if not asked about it.
3. When you have enough data, call submit_finding with:
     - analysis    : direct answer to the question (be specific, quote values)
     - confidence  : 0.0–1.0 (low if data is ambiguous or partial)
     - key_facts   : concrete extracted facts as short strings
     - unexpected  : things you noticed that weren't asked about — other agents need this

Call submit_finding exactly once. Do not speculate beyond what the data shows.
"""


class MetricsAgent(SpecialistAgent):
    AGENT_NAME = "metrics"
    SPAN_NAME  = "specialist.metrics"
    MAX_ITER   = 6
    SYSTEM     = _SYSTEM

    def domain_tools(self, ctx: IncidentContext) -> list:
        return make_prometheus_tools(ctx.metrics)

    @observe(name="metrics-agent")
    def run(self, ctx, question):
        return super().run(ctx, question)
