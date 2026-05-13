import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Any

import config
from agentmetrics import metrics as agentmetrics
from perception.prometheus import ClusterMetrics
from agents.base import Diagnosis
from tracing.decisions import get_decision_log

logger = logging.getLogger(__name__)


@dataclass
class ActionPlan:
    action: str
    reason: str
    safe: bool
    params: dict[str, Any] = field(default_factory=dict)


class DecisionEngine:
    """Safety-layer decision engine.

    Iterates the LLM's suggested_actions in order and returns the first
    one that passes all safety gates. Never modifies cluster state itself.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_action_time: dict[str, float] = {}

    def select_action(
        self,
        diag: Diagnosis,
        metrics: ClusterMetrics,
        incident_id: str = "",
        human_override: bool = False,
    ) -> ActionPlan:
        dlog = get_decision_log()

        if not diag.suggested_actions:
            plan = ActionPlan(action="no_action", reason="no suggestions from LLM", safe=True)
            if incident_id:
                dlog.record(incident_id, "decision_engine", "no_action — no suggestions from LLM")
            return plan

        for candidate in diag.suggested_actions:
            plan = self._evaluate(candidate, diag, metrics, human_override=human_override)
            if plan.safe:
                logger.info(f"action selected: {plan.action} — {plan.reason}")
                if incident_id:
                    dlog.record(
                        incident_id,
                        "decision_engine",
                        f"selected: {plan.action}",
                        reasoning=plan.reason,
                        evidence={"confidence": diag.confidence, "severity": diag.severity, "params": plan.params},
                        confidence=diag.confidence,
                    )
                return plan
            logger.info(f"action blocked: {candidate} — {plan.reason}")
            if incident_id:
                dlog.record(
                    incident_id,
                    "decision_engine",
                    f"blocked: {candidate}",
                    reasoning=plan.reason,
                    evidence={"severity": diag.severity},
                )

        final = ActionPlan(
            action="no_action",
            reason="all suggested actions blocked by safety checks or cooldown",
            safe=True,
        )
        if incident_id:
            dlog.record(
                incident_id,
                "decision_engine",
                "no_action — all candidates blocked",
                reasoning=final.reason,
            )
        return final

    def record_executed(self, action: str):
        with self._lock:
            self._last_action_time[action] = time.time()

    def _evaluate(
        self,
        action: str,
        diag: Diagnosis,
        metrics: ClusterMetrics,
        human_override: bool = False,
    ) -> ActionPlan:
        # Confidence checks are skipped when the operator explicitly chose the action —
        # the human is taking responsibility, so we trust their judgement.
        skip_confidence = human_override

        if action == "rollback" and not skip_confidence and diag.confidence < config.ROLLBACK_MIN_CONF:
            agentmetrics.ACTIONS_BLOCKED.labels(action=action, reason="low_confidence").inc()
            return ActionPlan(
                action=action,
                reason=(
                    f"rollback requires confidence ≥{config.ROLLBACK_MIN_CONF*100:.0f}% "
                    f"(got {diag.confidence*100:.0f}%)"
                ),
                safe=False,
            )

        if action == "patch_code":
            if not config.ALLOW_CODE_PATCHES:
                agentmetrics.ACTIONS_BLOCKED.labels(action=action, reason="disabled").inc()
                return ActionPlan(
                    action=action,
                    reason="patch_code disabled — set ALLOW_CODE_PATCHES=true to enable",
                    safe=False,
                )
            if not skip_confidence and diag.confidence < config.PATCH_MIN_CONF:
                agentmetrics.ACTIONS_BLOCKED.labels(action=action, reason="low_confidence").inc()
                return ActionPlan(
                    action=action,
                    reason=(
                        f"patch_code requires confidence ≥{config.PATCH_MIN_CONF*100:.0f}% "
                        f"(got {diag.confidence*100:.0f}%)"
                    ),
                    safe=False,
                )

        if config.DRY_RUN:
            return ActionPlan(
                action=action,
                reason=f"[DRY RUN] would execute: {action}",
                safe=True,
                params=self._build_params(action, metrics),
            )

        # Cooldown gate
        with self._lock:
            last = self._last_action_time.get(action, 0.0)
        elapsed = time.time() - last
        if elapsed < config.COOLDOWN_SEC:
            remaining = int(config.COOLDOWN_SEC - elapsed)
            agentmetrics.ACTIONS_BLOCKED.labels(action=action, reason="cooldown").inc()
            return ActionPlan(
                action=action,
                reason=f"cooldown active for '{action}' ({remaining}s remaining)",
                safe=False,
            )

        current = metrics.desired_replicas or 2

        if action == "scale_up":
            if current >= config.MAX_REPLICAS:
                agentmetrics.ACTIONS_BLOCKED.labels(action=action, reason="max_replicas").inc()
                return ActionPlan(
                    action=action,
                    reason=f"already at max replicas ({config.MAX_REPLICAS})",
                    safe=False,
                )

        elif action == "scale_down":
            if current <= config.MIN_REPLICAS:
                agentmetrics.ACTIONS_BLOCKED.labels(action=action, reason="min_replicas").inc()
                return ActionPlan(
                    action=action,
                    reason=f"already at min replicas ({config.MIN_REPLICAS})",
                    safe=False,
                )
            if diag.severity in ("critical", "high"):
                agentmetrics.ACTIONS_BLOCKED.labels(action=action, reason="severity").inc()
                return ActionPlan(
                    action=action,
                    reason="scale-down blocked during critical/high incident",
                    safe=False,
                )

        return ActionPlan(
            action=action,
            reason=f"root cause: {diag.root_cause}",
            safe=True,
            params=self._build_params(action, metrics),
        )

    def _build_params(self, action: str, metrics: ClusterMetrics) -> dict:
        current = metrics.desired_replicas or 2
        if action == "scale_up":
            return {"replicas": min(current + 1, config.MAX_REPLICAS)}
        if action == "scale_down":
            return {"replicas": max(current - 1, config.MIN_REPLICAS)}
        return {}
