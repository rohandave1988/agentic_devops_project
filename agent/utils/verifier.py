import logging
import time

import config
from agentmetrics import metrics as agentmetrics
from utils.slo import check_slos

logger = logging.getLogger(__name__)


def verify_and_record(
    incident_id: str,
    action: str,
    incident_start: float,
    prom,
    store,
) -> None:
    """Re-check SLOs after config.VERIFY_DELAY_SEC, record outcome.

    Runs in a daemon thread so it does not block the main agent loop.
    """
    logger.info(
        f"verification pending: incident={incident_id} action={action} "
        f"wait={config.VERIFY_DELAY_SEC}s"
    )
    time.sleep(config.VERIFY_DELAY_SEC)

    try:
        metrics = prom.collect_metrics()
    except Exception as e:
        logger.error(f"verification metrics fetch failed: incident={incident_id} err={e}")
        agentmetrics.VERIFICATIONS.labels(action=action, result="error").inc()
        return

    violations = check_slos(metrics)
    recovered  = len(violations) == 0
    mttr_sec   = int(time.time() - incident_start)

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
            f"incident={incident_id} action={action} elapsed_sec={mttr_sec}"
        )
        agentmetrics.VERIFICATIONS.labels(action=action, result="unresolved").inc()

    store.update_outcome(incident_id, recovered, mttr_sec)
