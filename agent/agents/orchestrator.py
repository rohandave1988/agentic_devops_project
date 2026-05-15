"""OrchestratorAgent — single-call LLM diagnosis.

Replaces the fragile multi-agent ReAct loop with a single LLM call that
receives all context (metrics + logs + history) pre-fetched and asks for
a structured JSON diagnosis.

Why: the multi-hop tool-calling loop (Orchestrator → MetricsAgent → LogsAgent)
made 5-10 LLM calls per incident. Local models (Ollama) frequently respond
with text instead of tool calls at any step, causing the chain to fall back to
no_action. A single well-structured prompt is far more reliable.

Tool calling is preserved only in CodePatchAgent, where it is genuinely needed
(reading source files before proposing a fix).
"""
import json
import re
import time

from langchain_core.messages import HumanMessage, SystemMessage

import config
from agents.base import Diagnosis
from agents.llm import build_llm
from agents.memory import AgentMemory
from logging_setup import get_logger
from perception.loki import format_for_llm
from tracing.decisions import get_decision_log
from tracing.spans import agent_span, set_span_attrs
from agents.langfuse_utils import observe, langfuse_context
from agentmetrics import metrics as agentmetrics

_log = get_logger("orchestrator")

_VALID_ACTIONS = {"restart_pods", "scale_up", "scale_down", "rollback", "patch_code", "no_action"}
_ALIASES: dict[str, list[str]] = {
    "rollback":     ["roll back", "revert", "undo", "previous version"],
    "restart_pods": ["restart", "redeploy", "bounce", "kill pod", "delete pod"],
    "scale_up":     ["scale up", "increase replica", "add replica", "horizontal"],
    "scale_down":   ["scale down", "reduce replica", "decrease replica"],
    "patch_code":   ["patch", "fix code", "code fix", "write fix", "apply fix", "open pr", "pull request"],
    "no_action":    ["no action", "do nothing", "monitor only", "observe"],
}


def _normalise(raw: str) -> str:
    a = raw.strip().lower()
    if a in _VALID_ACTIONS:
        return a
    for canonical, aliases in _ALIASES.items():
        if any(alias in a for alias in aliases):
            return canonical
    return "no_action"


_SYSTEM = """\
You are a senior SRE diagnosing a Kubernetes production incident.
You will receive current metrics, recent error logs, and past incident history.
Your job: identify the root cause and recommend the single best remediation action.

Valid actions — choose EXACTLY one:
  restart_pods  — pod crash loop, bad process state, or exceptions on every request that infra can clear
  scale_up      — high CPU or high latency under load caused by insufficient replicas
  scale_down    — over-provisioned, CPU/memory well below SLO, wasting resources
  rollback      — 4xx surge (not 5xx) after a recent deploy; broken auth or config
  patch_code    — a specific named exception is visible in logs (ZeroDivisionError, IndexError, KeyError, TypeError, etc.)
  no_action     — false positive; all metrics are within SLO; self-healing already in progress

Decision rules (apply in order):
1. Logs show a named Python exception (ZeroDivisionError, IndexError, etc.)  → patch_code
2. 5xx error rate is high AND no named exception in logs                      → restart_pods
3. P99 latency above SLO AND CPU usage above 60%                              → scale_up
4. 4xx error rate is high (not 5xx)                                           → rollback
5. All SLOs healthy                                                            → no_action

Respond with ONLY a valid JSON object — no markdown fences, no explanation:
{
  "action": "<valid action>",
  "root_cause": "<concise description, max 20 words>",
  "severity": "<critical|high|medium|low>",
  "confidence": <0.0-1.0>,
  "reasoning": "<how metrics and logs support this action, max 40 words>"
}
"""

_TASK = """\
SLO Violations detected:
{violations}

Current Metrics:
  5xx error rate   : {error_rate:.2f}%    (SLO < {slo_error:.0f}%,  {breach_5xx})
  4xx error rate   : {http_4xx:.2f}%      (SLO < {slo_4xx:.0f}%,    {breach_4xx})
  P99 latency      : {p99:.0f} ms         (SLO < {slo_p99:.0f} ms,  {breach_p99})
  CPU usage        : {cpu:.1f}%
  Memory usage     : {mem:.1f}%
  Pod restarts     : {restarts}           (last 5 min)
  OOM kills        : {oom}
  Ready replicas   : {ready}/{desired}

Recent application logs (last 2 minutes):
{logs}

Past incident history (last 5 incidents):
{history}

Diagnose the incident. Reply with JSON only.
"""


def _fmt_history(store) -> str:
    try:
        rows = store.get_recent(n=5)
        if not rows:
            return "(no history)"
        lines = []
        for r in rows:
            lines.append(
                f"  cause={r.get('root_cause','?')!r}  "
                f"action={r.get('action_taken','?')}  "
                f"recovered={r.get('slo_recovered', False)}"
            )
        return "\n".join(lines)
    except Exception:
        return "(history unavailable)"


def _parse_diagnosis(text: str, violations: list[str]) -> Diagnosis | None:
    """Extract JSON from LLM response; returns None if parsing fails."""
    # Strip markdown fences if present
    clean = re.sub(r"```(?:json)?", "", text).strip()

    # Try to extract a JSON object
    match = re.search(r"\{[\s\S]*\}", clean)
    if not match:
        return None

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    action = _normalise(str(data.get("action", "")))
    sev = str(data.get("severity", "medium")).lower().strip()
    if sev not in ("critical", "high", "medium", "low"):
        sev = "medium"
    confidence = min(max(float(data.get("confidence", 0.5)), 0.0), 1.0)

    return Diagnosis(
        root_cause=str(data.get("root_cause", "unknown"))[:200],
        severity=sev,
        suggested_actions=[action],
        confidence=confidence,
        anomalies=violations,
        reasoning=str(data.get("reasoning", ""))[:300],
    )


def _fallback(violations: list[str]) -> Diagnosis:
    return Diagnosis(
        anomalies=violations,
        root_cause="investigation incomplete — could not determine root cause",
        severity="medium",
        suggested_actions=["no_action"],
        confidence=0.1,
        reasoning="fallback: LLM did not produce a parseable diagnosis",
    )


class OrchestratorAgent:
    def __init__(self):
        self._llm    = build_llm(max_tokens=512)
        self._memory = AgentMemory("orchestrator")
        _log.info("orchestrator ready")

    @observe(name="investigate")
    def investigate(
        self,
        violations: list[str],
        metrics,
        loki,
        store,
        incident_id: str,
    ) -> Diagnosis:
        langfuse_context.update_current_observation(
            input={
                "violations":   violations,
                "error_rate":   round(metrics.error_rate * 100, 2),
                "latency_p99":  round(metrics.latency_p99_ms, 1),
                "pod_restarts": metrics.pod_restarts,
            },
        )

        dlog = get_decision_log()
        dlog.record(
            incident_id, "orchestrator",
            f"investigation started — {len(violations)} violation(s)",
            reasoning="; ".join(violations[:3]),
        )

        # ── 1. Pre-fetch all context (no LLM yet) ─────────────────────────────
        with agent_span("agent.gather_context", **{"incident.id": incident_id}):
            try:
                log_lines = loki.query_recent_logs(lookback_sec=120)
            except Exception:
                log_lines = []

            # Promtail typically lags 15–30 s behind pod stderr.  If the first
            # query returns nothing on a live breach, wait once and retry with a
            # wider window so the exception stack trace reaches the LLM.
            if not log_lines:
                _log.info(
                    "Loki returned no logs — waiting 20s for Promtail scraping lag",
                    extra={"incident_id": incident_id},
                )
                time.sleep(20)
                try:
                    log_lines = loki.query_recent_logs(lookback_sec=300)
                except Exception:
                    log_lines = []

            logs_text = format_for_llm(log_lines)
            history_text = _fmt_history(store)

        agentmetrics.SPECIALIST_CALLS.labels(specialist="metrics").inc()
        agentmetrics.SPECIALIST_CALLS.labels(specialist="logs").inc()

        # ── 2. Build prompt ────────────────────────────────────────────────────
        task = _TASK.format(
            violations="\n".join(f"  • {v}" for v in violations),
            error_rate=metrics.error_rate * 100,
            slo_error=config.SLO_ERROR_RATE * 100,
            breach_5xx="BREACH" if metrics.error_rate > config.SLO_ERROR_RATE else "OK",
            http_4xx=metrics.http_4xx_rate * 100,
            slo_4xx=config.SLO_4XX_RATE * 100,
            breach_4xx="BREACH" if metrics.http_4xx_rate > config.SLO_4XX_RATE else "OK",
            p99=metrics.latency_p99_ms,
            slo_p99=config.SLO_LATENCY_MS,
            breach_p99="BREACH" if metrics.latency_p99_ms > config.SLO_LATENCY_MS else "OK",
            cpu=metrics.cpu_usage * 100,
            mem=metrics.memory_usage * 100,
            restarts=metrics.pod_restarts,
            oom=metrics.oom_kills,
            ready=metrics.ready_replicas,
            desired=metrics.desired_replicas,
            logs=logs_text,
            history=history_text,
        )

        messages = [SystemMessage(content=_SYSTEM), HumanMessage(content=task)]
        _model = config.OLLAMA_MODEL if config.LLM_BACKEND == "ollama" else config.CLAUDE_MODEL

        # ── 3. Single LLM call ─────────────────────────────────────────────────
        t0 = time.monotonic()
        _log.info(
            f"invoking LLM for diagnosis — {len(violations)} violation(s)",
            extra={"incident_id": incident_id, "model": _model},
        )
        with agent_span(
            "orchestrator.reason",
            **{"incident.id": incident_id, "llm.model": _model, "llm.provider": config.LLM_BACKEND},
        ):
            try:
                response = self._llm.invoke(messages)
                raw = response.content if hasattr(response, "content") else str(response)
            except Exception as e:
                _log.error(f"LLM call failed: {e}", extra={"incident_id": incident_id})
                return _fallback(violations)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log.debug(f"LLM responded in {elapsed_ms}ms", extra={"incident_id": incident_id})

        # ── 4. Parse JSON response ─────────────────────────────────────────────
        diag = _parse_diagnosis(raw, violations)

        if diag is None:
            _log.warning(
                "LLM response not parseable — fallback",
                extra={"incident_id": incident_id, "raw": raw[:200]},
            )
            dlog.record(
                incident_id, "orchestrator",
                "WARN: unparseable response — fallback to no_action",
                reasoning=raw[:200],
            )
            return _fallback(violations)

        # ── 5. Confidence adjustment: boost if logs contain named exception ────
        # The LLM sometimes undersells confidence when logs are clear. If we
        # can see a named exception and the action is patch_code, floor at 0.8.
        exception_pattern = re.compile(
            r"\b([A-Z][a-zA-Z]+(?:Error|Exception))\b", re.MULTILINE
        )
        named_exceptions = exception_pattern.findall(logs_text)
        if named_exceptions and diag.suggested_actions[0] == "patch_code":
            diag = Diagnosis(
                root_cause=diag.root_cause,
                severity=diag.severity,
                suggested_actions=diag.suggested_actions,
                confidence=max(diag.confidence, 0.80),
                anomalies=diag.anomalies,
                reasoning=diag.reasoning,
            )

        agentmetrics.INVESTIGATION_DURATION.observe(time.monotonic() - t0)

        dlog.record(
            incident_id, "orchestrator",
            f"diagnosis: {diag.suggested_actions[0]}, severity={diag.severity}",
            reasoning=diag.reasoning,
            evidence={
                "root_cause":  diag.root_cause,
                "action":      diag.suggested_actions[0],
                "named_exceptions": named_exceptions[:3],
            },
            confidence=diag.confidence,
        )

        _log.info(
            f"diagnosis ready — {diag.severity.upper()} | {diag.suggested_actions[0]} "
            f"| confidence={diag.confidence:.0%} | {diag.root_cause[:80]}",
            extra={
                "incident_id": incident_id,
                "action":      diag.suggested_actions[0],
                "confidence":  round(diag.confidence, 2),
                "elapsed_ms":  elapsed_ms,
            },
        )

        langfuse_context.update_current_observation(
            output={
                "action":     diag.suggested_actions[0],
                "severity":   diag.severity,
                "confidence": round(diag.confidence, 2),
                "root_cause": diag.root_cause,
            },
        )

        self._memory.remember(
            incident_id,
            question=f"violations: {'; '.join(violations[:3])}",
            key_facts=[
                f"action={diag.suggested_actions[0]}",
                f"severity={diag.severity}",
                f"confidence={diag.confidence:.0%}",
            ],
            unexpected=[],
        )

        return diag

    def record_outcome(
        self,
        incident_id: str,
        root_cause: str,
        action: str,
        recovered: bool,
        mttr_sec: int,
    ) -> None:
        outcome = "RECOVERED" if recovered else "FAILED"
        self._memory.remember(
            incident_id=incident_id,
            question=f"outcome: {root_cause}",
            key_facts=[
                f"action={action}",
                f"outcome={outcome}",
                f"mttr={mttr_sec}s" if recovered else f"unresolved after {mttr_sec}s",
            ],
            unexpected=(
                [f"action '{action}' DID NOT resolve: {root_cause}"] if not recovered else []
            ),
        )
        _log.info(
            "outcome recorded in memory",
            extra={"incident_id": incident_id, "action": action, "recovered": recovered},
        )
