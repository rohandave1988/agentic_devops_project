"""Escalation webhook — fires when the agent cannot self-heal.

Supports Slack incoming webhooks (detected by URL containing 'hooks.slack.com')
and any generic HTTP endpoint that accepts a JSON POST body.

Triggered from verifier.py after ESCALATION_AFTER_FAILURES consecutive
unresolved incidents within a rolling 30-minute window.
"""
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime

import config

logger = logging.getLogger(__name__)


def maybe_escalate(incident_id: str, action: str, mttr_sec: int, store) -> bool:
    """Fire escalation webhook if unresolved count exceeds threshold.

    Returns True if webhook was sent, False if skipped.
    """
    if not config.ESCALATION_WEBHOOK_URL:
        return False

    unresolved = store.count_unresolved_recent(window_minutes=30)
    if unresolved < config.ESCALATION_FAILURES:
        return False

    history = store.get_recent(5)
    _send(incident_id, action, mttr_sec, unresolved, history)
    return True


def _send(incident_id: str, action: str, elapsed_sec: int,
          unresolved_count: int, history: list[dict]):
    url = config.ESCALATION_WEBHOOK_URL
    ts  = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    if "hooks.slack.com" in url:
        payload = _slack_payload(incident_id, action, elapsed_sec, unresolved_count, ts)
    else:
        payload = _generic_payload(incident_id, action, elapsed_sec, unresolved_count, history, ts)

    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.warning(
                f"escalation fired: incident={incident_id} unresolved={unresolved_count} "
                f"http_status={resp.status}"
            )
    except urllib.error.URLError as e:
        logger.error(f"escalation webhook failed: {e}")


def _slack_payload(incident_id: str, action: str, elapsed_sec: int,
                   unresolved_count: int, ts: str) -> dict:
    return {
        "text": f":rotating_light: *Self-healing agent needs human help* — {ts}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🚨 Agent Escalation — Human Intervention Required"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Namespace:*\n`{config.TARGET_NAMESPACE}`"},
                    {"type": "mrkdwn", "text": f"*Deployment:*\n`{config.TARGET_DEPLOYMENT}`"},
                    {"type": "mrkdwn", "text": f"*Last incident:*\n`{incident_id}`"},
                    {"type": "mrkdwn", "text": f"*Last action tried:*\n`{action}`"},
                    {"type": "mrkdwn", "text": f"*Elapsed:*\n{elapsed_sec}s"},
                    {"type": "mrkdwn", "text": f"*Unresolved (30 min):*\n{unresolved_count}"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"The agent has had *{unresolved_count} consecutive unresolved incidents* "
                        f"in the last 30 minutes and cannot self-heal. Manual investigation required."
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Self-Healing Agent · {ts}"}],
            },
        ],
    }


def _generic_payload(incident_id: str, action: str, elapsed_sec: int,
                     unresolved_count: int, history: list[dict], ts: str) -> dict:
    return {
        "event":            "escalation",
        "timestamp":        ts,
        "namespace":        config.TARGET_NAMESPACE,
        "deployment":       config.TARGET_DEPLOYMENT,
        "incident_id":      incident_id,
        "last_action":      action,
        "elapsed_sec":      elapsed_sec,
        "unresolved_30min": unresolved_count,
        "threshold":        config.ESCALATION_FAILURES,
        "recent_history":   history,
        "message": (
            f"Agent escalating after {unresolved_count} unresolved incidents "
            f"in 30 min. Human intervention required."
        ),
    }
