import logging

from perception.prometheus import ClusterMetrics
from perception.loki import LokiClient
from reasoning.llm import LLMClient, Diagnosis
from reasoning.tool_runner import ToolRunner
from reasoning.tools import DIAGNOSIS_TOOLS

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are an expert Site Reliability Engineer (SRE) AI agent embedded in a Kubernetes self-healing system.

You have been called because SLO violations were detected. Your job is to investigate, identify the root cause, and recommend the best remediation.

Investigation process:
1. Call get_metrics to see exact metric values and which SLOs are breached.
2. Call get_recent_logs to look for error messages, stack traces, or anomalies.
3. Call get_incident_history to see what actions were taken recently and whether they worked.
4. Once you have enough evidence, call submit_diagnosis with your findings.

You may call tools in any order. Skip tools you don't need if the evidence is already clear.

Remediation action rules (for suggested_actions in submit_diagnosis):
- CPU > 80%           → scale_up is primary
- Memory > 85%        → restart_pods first, scale_up as fallback
- Error rate > 1%     → restart_pods; rollback ONLY if a recent bad deploy is the cause
- Pod restarts > 3    → restart_pods
- rollback            → ONLY for bad code deployments, NEVER for resource pressure
- scale_down          → ONLY at low severity when clearly over-provisioned
- Check history       → if an action was tried recently and SLOs did NOT recover, suggest a different one

Call submit_diagnosis when confident."""


class Analyzer:
    def __init__(self):
        self._llm = LLMClient()

    def analyze(
        self,
        violations: list[str],
        metrics: ClusterMetrics,
        loki: LokiClient,
        store,
    ) -> Diagnosis:
        runner = ToolRunner(metrics, loki, store)
        user_msg = (
            "SLO violations detected:\n"
            + "\n".join(violations)
            + "\n\nInvestigate and submit your diagnosis."
        )
        logger.info(f"starting LLM investigation: {len(violations)} violation(s)")
        return self._llm.complete_with_tools(_SYSTEM_PROMPT, user_msg, DIAGNOSIS_TOOLS, runner)
