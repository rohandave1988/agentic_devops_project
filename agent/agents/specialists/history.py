"""HistoryAgent — incident history specialist."""
from agents.base import IncidentContext
from agents.specialists._base import SpecialistAgent
from agents.tools.history import make_history_tools
from agents.langfuse_utils import observe

_SYSTEM = """\
You are an incident history specialist agent for a production Kubernetes service.
You have persistent memory of your past analyses — use it to detect recurring patterns.

Your domain tools:
  get_recent_incidents  — last N incidents: causes, actions, outcomes
  get_action_outcomes   — success/failure counts per remediation action
  get_metric_trend      — metric deltas over a time window

Investigation rules:
1. Answer the specific question asked — but scan for recurring patterns too.
2. If an action was tried recently and failed, flag this prominently as a key_fact.
3. If you see a trend worsening (metric_trend going up), flag it as unexpected even if not asked.
4. Be direct about action recommendations: "rollback worked 3/3 times for AuthError" is more
   useful than "rollback might be considered."
5. Call submit_finding exactly once with structured output.
"""


class HistoryAgent(SpecialistAgent):
    AGENT_NAME = "history"
    SPAN_NAME  = "specialist.history"
    MAX_ITER   = 5
    SYSTEM     = _SYSTEM

    def domain_tools(self, ctx: IncidentContext) -> list:
        return make_history_tools(ctx.store)

    @observe(name="history-agent")
    def run(self, ctx, question):
        return super().run(ctx, question)
