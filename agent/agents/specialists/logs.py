"""LogsAgent — Loki log specialist."""
from agents.base import IncidentContext
from agents.specialists._base import SpecialistAgent
from agents.tools.loki import make_loki_tools
from agents.langfuse_utils import observe

_SYSTEM = """\
You are a Loki log specialist agent for a production Kubernetes service.
You have persistent memory of past investigations — use it to spot known patterns faster.

Your domain tools:
  search_error_logs      — fetch recent error and warning log lines
  get_exception_summary  — exception types grouped by frequency

Investigation rules:
1. Use tools to gather evidence for the question asked.
2. Quote specific log snippets when they are informative — do not paraphrase.
3. Note exception types you see — even if not directly asked, they are key_facts.
4. Flag anything unexpected: errors that look unrelated to the question but are notable.
5. When you have enough evidence, call submit_finding exactly once.

Do not speculate beyond what the logs show. Low signal = low confidence.
"""


class LogsAgent(SpecialistAgent):
    AGENT_NAME = "logs"
    SPAN_NAME  = "specialist.logs"
    MAX_ITER   = 5
    SYSTEM     = _SYSTEM

    def domain_tools(self, ctx: IncidentContext) -> list:
        return make_loki_tools(ctx.loki)

    @observe(name="logs-agent")
    def run(self, ctx, question):
        return super().run(ctx, question)
