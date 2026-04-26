"""Unit tests for DecisionEngine — mirrors planning/decision_test.go exactly."""
import time
import sys
import os

# Ensure agent-python root is on the path when running from any directory
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config

# Override defaults so tests are deterministic regardless of env
config.DRY_RUN           = False
config.MAX_REPLICAS       = 6
config.MIN_REPLICAS       = 1
config.COOLDOWN_SEC       = 120
config.ROLLBACK_MIN_CONF  = 0.6

from perception.prometheus import ClusterMetrics
from reasoning.llm import Diagnosis
from planning.decision import DecisionEngine


# ── Helpers ───────────────────────────────────────────────────────────────────

def healthy_metrics(**overrides) -> ClusterMetrics:
    m = ClusterMetrics(
        error_rate=0.005,
        latency_p99_ms=80,
        cpu_usage=0.40,
        memory_usage=0.50,
        pod_restarts=0,
        ready_replicas=2,
        desired_replicas=2,
    )
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


def diag(actions: list[str], severity: str = "high", confidence: float = 0.9) -> Diagnosis:
    return Diagnosis(
        suggested_actions=actions,
        severity=severity,
        confidence=confidence,
        anomalies=[],
        root_cause="test",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_no_suggestions():
    plan = DecisionEngine().select_action(diag([]), healthy_metrics())
    assert plan.action == "no_action"


def test_allows_valid_action():
    plan = DecisionEngine().select_action(diag(["scale_up"]), healthy_metrics(desired_replicas=2))
    assert plan.action == "scale_up"
    assert plan.safe


def test_cooldown_blocks_repeat():
    d = DecisionEngine()
    m = healthy_metrics(desired_replicas=2)

    first = d.select_action(diag(["restart_pods"]), m)
    assert first.action == "restart_pods" and first.safe
    d.record_executed("restart_pods")

    second = d.select_action(diag(["restart_pods"]), m)
    assert not (second.action == "restart_pods" and second.safe), \
        "second action within cooldown should be blocked"


def test_scale_up_blocked_at_max_replicas():
    plan = DecisionEngine().select_action(
        diag(["scale_up"]), healthy_metrics(desired_replicas=6)
    )
    assert not (plan.safe and plan.action == "scale_up")


def test_scale_down_blocked_at_min_replicas():
    plan = DecisionEngine().select_action(
        diag(["scale_down"], severity="low"), healthy_metrics(desired_replicas=1)
    )
    assert not (plan.safe and plan.action == "scale_down")


def test_scale_down_blocked_during_critical():
    plan = DecisionEngine().select_action(
        diag(["scale_down"], severity="critical"), healthy_metrics(desired_replicas=3)
    )
    assert not (plan.safe and plan.action == "scale_down")


def test_scale_down_blocked_during_high():
    plan = DecisionEngine().select_action(
        diag(["scale_down"], severity="high"), healthy_metrics(desired_replicas=3)
    )
    assert not (plan.safe and plan.action == "scale_down")


def test_rollback_blocked_low_confidence():
    plan = DecisionEngine().select_action(
        diag(["rollback"], confidence=0.5), healthy_metrics()
    )
    assert not (plan.safe and plan.action == "rollback")


def test_rollback_allowed_sufficient_confidence():
    plan = DecisionEngine().select_action(
        diag(["rollback"], confidence=0.8), healthy_metrics()
    )
    assert plan.safe and plan.action == "rollback"


def test_fallback_to_second_suggestion():
    # scale_up blocked at max replicas → should fall through to restart_pods
    plan = DecisionEngine().select_action(
        diag(["scale_up", "restart_pods"]), healthy_metrics(desired_replicas=6)
    )
    assert plan.action == "restart_pods"


def test_cooldown_expires():
    d = DecisionEngine()
    m = healthy_metrics(desired_replicas=2)
    with d._lock:
        d._last_action_time["restart_pods"] = time.time() - 300  # well past cooldown

    plan = d.select_action(diag(["restart_pods"]), m)
    assert plan.safe and plan.action == "restart_pods"
