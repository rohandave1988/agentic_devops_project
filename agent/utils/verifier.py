import logging
import time

import config
from agentmetrics import metrics as agentmetrics
from utils.slo import check_slos
from utils.escalation import maybe_escalate
from tracing.decisions import get_decision_log

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 10  # seconds between each readiness + SLO check


def _wait_for_recovery(action: str, incident_id: str, prom) -> tuple[bool, list[str]]:
    """Poll until SLOs clear or VERIFY_MAX_WAIT_SEC elapses.

    For scale/restart actions, first waits for replicas to be ready before
    checking SLOs — avoids false "unresolved" verdicts during pod startup.
    Returns (recovered, remaining_violations).
    """
    deadline = time.monotonic() + config.VERIFY_MAX_WAIT_SEC
    elapsed  = 0

    # Initial grace period — give Kubernetes time to start the action
    time.sleep(min(config.VERIFY_DELAY_SEC, config.VERIFY_MAX_WAIT_SEC))

    scale_actions = {"restart_pods", "scale_up", "scale_down"}

    while time.monotonic() < deadline:
        try:
            metrics = prom.collect_metrics()
        except Exception as e:
            logger.warning(f"verification poll failed: incident={incident_id} err={e}")
            time.sleep(_POLL_INTERVAL)
            continue

        # For scale-type actions: wait until replicas are ready before SLO check.
        if action in scale_actions and metrics.ready_replicas < metrics.desired_replicas:
            remaining = metrics.desired_replicas - metrics.ready_replicas
            logger.info(
                f"verification: waiting for pods — {remaining} replica(s) not ready yet "
                f"(incident={incident_id})"
            )
            time.sleep(_POLL_INTERVAL)
            continue

        violations = check_slos(metrics)
        if not violations:
            return True, []

        elapsed = int(time.monotonic() - (deadline - config.VERIFY_MAX_WAIT_SEC))
        logger.info(
            f"verification: SLOs still violated after {elapsed}s — "
            f"violations={violations[:2]} (incident={incident_id})"
        )
        time.sleep(_POLL_INTERVAL)

    # Timed out — do one final check
    try:
        metrics = prom.collect_metrics()
        violations = check_slos(metrics)
        return len(violations) == 0, violations
    except Exception:
        return False, ["metrics unavailable at deadline"]


def verify_and_record(
    incident_id: str,
    action: str,
    incident_start: float,
    prom,
    store,
    orchestrator=None,
    root_cause: str = "",
) -> None:
    """Re-check SLOs after action, poll until recovered or VERIFY_MAX_WAIT_SEC elapses.

    Runs in a daemon thread so it does not block the main agent loop.
    Fires escalation webhook when consecutive unresolved incidents breach threshold.
    Calls orchestrator.record_outcome() to close the diagnosis accuracy feedback loop.
    """
    logger.info(
        f"verification pending: incident={incident_id} action={action} "
        f"initial_wait={config.VERIFY_DELAY_SEC}s max_wait={config.VERIFY_MAX_WAIT_SEC}s"
    )

    recovered, violations = _wait_for_recovery(action, incident_id, prom)
    mttr_sec = int(time.time() - incident_start)

    if recovered:
        logger.info(
            f"SLOs recovered — incident closed: incident={incident_id} "
            f"action={action} mttr_sec={mttr_sec}"
        )
        agentmetrics.MTTR.observe(float(mttr_sec))
        agentmetrics.VERIFICATIONS.labels(action=action, result="resolved").inc()
    else:
        logger.warning(
            f"SLOs still violated after action — incident remains open: "
            f"incident={incident_id} action={action} elapsed_sec={mttr_sec} "
            f"violations={violations}"
        )
        agentmetrics.VERIFICATIONS.labels(action=action, result="unresolved").inc()

    store.update_outcome(incident_id, recovered, mttr_sec)

    # ── Record verification decision + close the decision chain ──────────────
    dlog = get_decision_log()
    dlog.record(
        incident_id,
        "verification",
        f"{'RECOVERED' if recovered else 'UNRESOLVED'} in {mttr_sec}s — action={action}",
        reasoning=f"violations_after={violations if not recovered else 'none'}",
        evidence={
            "action":          action,
            "mttr_sec":        mttr_sec,
            "violations_after": violations[:3] if not recovered else [],
        },
        confidence=1.0,
    )
    dlog.record_outcome(incident_id, "correct" if recovered else "incorrect")

    # ── Close the feedback loop: tell the orchestrator whether its diagnosis was right ──
    if orchestrator is not None:
        try:
            orchestrator.record_outcome(incident_id, root_cause, action, recovered, mttr_sec)
        except Exception as exc:
            logger.warning(f"orchestrator.record_outcome failed: {exc}")

    if not recovered:
        escalated = maybe_escalate(incident_id, action, mttr_sec, store)
        if escalated:
            logger.warning(
                f"escalation webhook fired: incident={incident_id} "
                f"unresolved_threshold={config.ESCALATION_FAILURES}"
            )
