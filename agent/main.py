"""Self-Healing Agent — Python implementation.
Full agentic loop: Perceive → Reason → Plan → Act → Verify → Remember

Run:
    python main.py
    DRY_RUN=true python main.py
"""
import os
import signal
import sys
import threading
import time

import config
import logging_setup
from tracing import setup_tracing
from tracing.spans import agent_span, set_span_attrs

from agentmetrics import metrics as agentmetrics
from perception.prometheus import PrometheusClient
from perception.loki import LokiClient
from planning.decision import DecisionEngine
from agents.orchestrator import OrchestratorAgent
from action.executor import Executor
from memory.store import Store
from utils.slo import check_slos
from utils.slo_tracker import SLOStateTracker
from utils.verifier import verify_and_record
from utils.render import print_banner, print_divider, print_metrics_table, print_violations, print_diagnosis, print_plan
from tracing.decisions import get_decision_log
from agents.langfuse_utils import langfuse_context, observe

logger = logging_setup.get_logger("main")


# ── Agent ─────────────────────────────────────────────────────────────────────

class Agent:
    def __init__(self):
        self.prometheus   = PrometheusClient()
        self.loki         = LokiClient()
        self.orchestrator = OrchestratorAgent()
        self.decision     = DecisionEngine()
        self.executor     = Executor()
        self.store        = Store()
        self.slo_tracker  = SLOStateTracker()
        self.cycle        = 0

    def run(self):
        print_banner()
        stop = threading.Event()

        def _on_signal(sig, _frame):
            logger.info("shutdown signal received — stopping agent")
            stop.set()

        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT,  _on_signal)

        self._run_cycle()
        while not stop.is_set():
            stop.wait(timeout=config.POLL_INTERVAL)
            if not stop.is_set():
                self._run_cycle()

    def _run_cycle(self):
        self.cycle += 1
        agentmetrics.CYCLES.inc()
        print_divider(f"Agent Cycle #{self.cycle}")

        with agent_span("agent.cycle", **{"cycle.number": self.cycle}) as cycle_sp:

            # ── 1. PERCEIVE ──────────────────────────────────────────────────
            logger.info("collecting metrics")
            try:
                with agent_span("agent.perceive"):
                    metrics = self.prometheus.collect_metrics()
            except Exception as e:
                logger.error(f"metrics collection failed: {e}")
                return

            # ── 2. SLO CHECK ─────────────────────────────────────────────────
            violations = check_slos(metrics)
            self.store.insert_metric_snapshot(metrics)
            print_metrics_table(metrics)
            agentmetrics.THROUGHPUT.observe(metrics.requests_per_second)

            cycle_sp.set_attribute("slo.violated", bool(violations))
            cycle_sp.set_attribute("violations.count", len(violations))

            if not violations:
                agentmetrics.SLO_CHECKS.labels(result="healthy").inc()
                logger.info("all SLOs healthy — no LLM analysis needed")
                return

            # Hysteresis: violations must persist for SLO_SUSTAINED_SEC before acting.
            # Prevents spurious investigations from single-scrape spikes.
            sustained = self.slo_tracker.update(violations)
            pending   = self.slo_tracker.pending()
            if pending:
                logger.info(
                    f"SLO violation seen but not yet sustained — waiting: "
                    + ", ".join(f"{k} ({v}s)" for k, v in pending.items())
                )
            if not sustained:
                agentmetrics.SLO_CHECKS.labels(result="transient").inc()
                return
            violations = sustained  # only act on sustained violations

            incident_start = time.time()
            agentmetrics.SLO_CHECKS.labels(result="violated").inc()
            print_violations(violations)
            self._handle_incident(violations, metrics, incident_start, cycle_sp)

    @observe(name="incident")
    def _handle_incident(self, violations, metrics, incident_start, cycle_sp):
        # ── 3. REASON ────────────────────────────────────────────────────
        incident_id = f"run-{time.time_ns()}"
        langfuse_context.update_current_trace(
            name=f"incident-{incident_id}",
            input={
                "violations":  violations,
                "error_rate":  round(metrics.error_rate * 100, 2),
                "latency_p99": round(metrics.latency_p99_ms, 1),
            },
        )
        get_decision_log().record(
            incident_id, "slo_check",
            f"{len(violations)} violation(s) triggered investigation",
            reasoning="; ".join(violations[:5]),
            evidence={
                "violations":     violations,
                "error_rate":     round(metrics.error_rate, 4),
                "latency_p99_ms": round(metrics.latency_p99_ms, 1),
                "pod_restarts":   metrics.pod_restarts,
            },
        )
        cycle_sp.set_attribute("incident.id", incident_id)
        logger.info("starting agent investigation")
        try:
            with agent_span(
                "agent.investigate",
                **{
                    "incident.id":      incident_id,
                    "violations.count": len(violations),
                    "violations.list":  "; ".join(violations[:5]),
                },
            ) as inv_sp:
                diag = self.orchestrator.investigate(
                    violations, metrics, self.loki, self.store, incident_id
                )
                set_span_attrs(inv_sp, **{
                    "diagnosis.action":     diag.suggested_actions[0],
                    "diagnosis.severity":   diag.severity,
                    "diagnosis.confidence": diag.confidence,
                })
        except Exception as e:
            logger.error(f"agent investigation failed: {e}")
            return
        print_diagnosis(diag)
        agentmetrics.INCIDENTS.labels(severity=diag.severity).inc()

        # ── 4. HUMAN REVIEW ──────────────────────────────────────────────
        if config.HUMAN_IN_LOOP:
            from hitl.review import prompt as hitl_prompt
            from dataclasses import replace
            ai_action = diag.suggested_actions[0] if diag.suggested_actions else "no_action"
            chosen    = hitl_prompt(diag, config.HUMAN_REVIEW_TIMEOUT)
            human_override = chosen != ai_action
            if human_override:
                diag = replace(
                    diag,
                    suggested_actions=[chosen] + [a for a in diag.suggested_actions if a != chosen],
                )
        else:
            human_override = False

        # ── 5. PLAN ──────────────────────────────────────────────────────
        with agent_span("agent.decide", **{"incident.id": incident_id}) as dec_sp:
            plan = self.decision.select_action(
                diag, metrics, incident_id=incident_id, human_override=human_override
            )
            set_span_attrs(dec_sp, **{
                "action.selected": plan.action,
                "action.safe":     plan.safe,
                "action.reason":   plan.reason[:200],
            })
        agentmetrics.DIAGNOSIS_CONFIDENCE.observe(diag.confidence)
        print_plan(plan)

        # ── 6. ACT ───────────────────────────────────────────────────────
        if plan.action == "patch_code":
            try:
                recent_logs = "\n".join(self.loki.query_recent_logs(lookback_sec=300)[:20])
            except Exception:
                recent_logs = ""
            plan.params.update({
                "root_cause":    diag.root_cause,
                "severity":      diag.severity,
                "confidence":    diag.confidence,
                "recent_logs":   recent_logs,
                "actions_tried": [],
                "incident_id":   incident_id,
            })

        action_taken  = False
        action_result = "skipped"

        if plan.action != "no_action" and plan.safe:
            result = self.executor.execute(plan)
            self.decision.record_executed(plan.action)
            action_result = result.status
            action_taken  = True
            agentmetrics.ACTIONS_EXECUTED.labels(action=plan.action).inc()

        cycle_sp.set_attribute("action.taken",  action_taken)
        cycle_sp.set_attribute("action.result", action_result)

        # ── 7. REMEMBER ──────────────────────────────────────────────────
        inc = self.store.record(
            anomalies=diag.anomalies,
            root_cause=diag.root_cause,
            severity=diag.severity,
            action=plan.action,
            result=action_result,
            confidence=diag.confidence,
            incident_id=incident_id,
            snapshot={
                "error_rate":      metrics.error_rate,
                "latency_p99_ms":  metrics.latency_p99_ms,
                "latency_p50_ms":  metrics.latency_p50_ms,
                "cpu_usage":       metrics.cpu_usage,
                "memory_usage":    metrics.memory_usage,
                "oom_kills":       metrics.oom_kills,
                "pod_restarts":    metrics.pod_restarts,
            },
        )

        # ── 8. VERIFY ────────────────────────────────────────────────────
        if action_taken and not config.DRY_RUN:
            threading.Thread(
                target=verify_and_record,
                args=(inc.id, plan.action, incident_start, self.prometheus, self.store),
                kwargs={"orchestrator": self.orchestrator, "root_cause": diag.root_cause},
                daemon=False,
            ).start()

        langfuse_context.update_current_trace(
            output={
                "action":     plan.action,
                "result":     action_result,
                "confidence": round(diag.confidence, 2),
                "root_cause": diag.root_cause,
            },
        )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logging_setup.setup()
    setup_tracing()

    if config.LLM_BACKEND == "claude" and not config.ANTHROPIC_KEY:
        logger.error("ANTHROPIC_API_KEY must be set when LLM_BACKEND=claude")
        sys.exit(1)

    agentmetrics.start_server(config.METRICS_PORT)

    try:
        agent = Agent()
    except Exception as e:
        logger.error(f"failed to initialise agent: {e}")
        sys.exit(1)

    agent.run()


if __name__ == "__main__":
    main()
