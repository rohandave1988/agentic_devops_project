"""Decision audit trail — records every reasoning step for quality inspection.

Answers two questions:
  1. What decision was made at each step of an investigation, and where?
  2. Was it the right decision? (verdict filled in when verification runs)

Different from OTel tracing:
  - OTel measures performance: duration, errors, latency.
  - DecisionLog measures reasoning quality: what was decided, why, was it correct.

One entry per decision point. Entries for an incident tell the complete story:

  step 1  slo_check          2 violations triggered investigation
  step 2  orchestrator       → ask_logs_agent: "4xx=87%, auth errors?"
  step 3  specialist.logs    finding: AuthError×47, confidence=88%
  step 4  orchestrator       → ask_history_agent: "rollback for AuthError?"
  step 5  evidence_gate      ALLOWED — 2 specialists consulted
  step 6  orchestrator       finalized: rollback, confidence=85%
  step 7  self_reflection    CONFIRMED (no revision)
  step 8  decision_engine    selected rollback (all gates passed)
  step 9  verification       RECOVERED in 47s ✓ CORRECT

Use show_chain(incident_id) to print this in the terminal.
"""
import json
import sqlite3
import threading
import time
from typing import Any

import config
from logging_setup import get_logger

_log = get_logger("decisions")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT    NOT NULL,
    ts          REAL    NOT NULL,
    component   TEXT    NOT NULL,   -- who made it
    decision    TEXT    NOT NULL,   -- what was decided (short label)
    reasoning   TEXT    DEFAULT '', -- why
    evidence    TEXT    DEFAULT '{}',-- JSON: available inputs at decision time
    confidence  REAL,               -- 0.0–1.0 if applicable
    outcome     TEXT    DEFAULT 'pending'  -- pending | correct | incorrect | unknown
);
CREATE INDEX IF NOT EXISTS idx_decision_log_incident ON decision_log(incident_id, ts);
"""

_COMPONENT_LABELS = {
    "slo_check":        "SLO Check",
    "orchestrator":     "Orchestrator",
    "specialist.metrics":"Metrics Agent",
    "specialist.logs":  "Logs Agent",
    "specialist.history":"History Agent",
    "evidence_gate":    "Evidence Gate",
    "self_reflection":  "Self-Reflection",
    "decision_engine":  "Decision Engine",
    "verification":     "Verification",
}


class DecisionLog:
    """Thread-safe SQLite-backed decision audit trail.

    One instance per process (use get_decision_log() singleton).
    Each record() call appends one step to the chain for an incident.
    """

    def __init__(self, db_path: str):
        self._db   = db_path
        self._lock = threading.Lock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ── Write API ──────────────────────────────────────────────────────────────

    def record(
        self,
        incident_id: str,
        component: str,
        decision: str,
        reasoning: str = "",
        evidence: dict[str, Any] | None = None,
        confidence: float | None = None,
    ) -> None:
        """Append one decision step to the chain for this incident."""
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO decision_log
                   (incident_id, ts, component, decision, reasoning, evidence, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    incident_id,
                    time.time(),
                    component,
                    decision[:300],
                    (reasoning or "")[:500],
                    json.dumps(evidence or {}, default=str),
                    confidence,
                ),
            )

    def record_outcome(self, incident_id: str, outcome: str) -> None:
        """Mark all steps for this incident with the final correctness verdict.

        outcome: "correct" | "incorrect" | "unknown"
        """
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE decision_log SET outcome=? WHERE incident_id=?",
                (outcome, incident_id),
            )
        _log.debug("outcome recorded", extra={"incident_id": incident_id, "outcome": outcome})

    # ── Read API ───────────────────────────────────────────────────────────────

    def get_chain(self, incident_id: str) -> list[dict]:
        """Return all decision steps for an incident, ordered by time."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM decision_log WHERE incident_id=? ORDER BY ts",
                (incident_id,),
            ).fetchall()
        result = []
        for i, row in enumerate(rows, 1):
            d = dict(row)
            d["step"] = i
            try:
                d["evidence"] = json.loads(d.get("evidence") or "{}")
            except Exception:
                d["evidence"] = {}
            result.append(d)
        return result

    def list_incidents(self, n: int = 10) -> list[dict]:
        """Return summary of recent incidents that have decision chains."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT incident_id,
                          MIN(ts) as started,
                          COUNT(*) as steps,
                          MAX(outcome) as outcome
                   FROM decision_log
                   GROUP BY incident_id
                   ORDER BY started DESC
                   LIMIT ?""",
                (n,),
            ).fetchall()
        return [dict(r) for r in rows]


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: DecisionLog | None = None
_init_lock = threading.Lock()


def get_decision_log() -> DecisionLog:
    """Return the process-level singleton DecisionLog."""
    global _instance
    if _instance is None:
        with _init_lock:
            if _instance is None:
                _instance = DecisionLog(config.DB_PATH)
    return _instance


# ── Terminal inspector ─────────────────────────────────────────────────────────

def show_chain(incident_id: str) -> None:
    """Print the full decision chain for an incident to the terminal.

    Usage:
        from tracing.decisions import show_chain
        show_chain("run-17431234567")
    """
    from rich.console import Console
    from rich.table import Table
    from rich import box
    from datetime import datetime, timezone

    console = Console()
    log     = get_decision_log()
    chain   = log.get_chain(incident_id)

    if not chain:
        console.print(f"[yellow]No decision chain found for incident: {incident_id}[/yellow]")
        return

    outcome_global = chain[-1].get("outcome", "pending") if chain else "pending"
    outcome_color  = {"correct": "green", "incorrect": "red", "pending": "yellow", "unknown": "dim"}.get(
        outcome_global, "white"
    )

    console.print()
    console.print(f"[bold cyan]{'═' * 68}[/bold cyan]")
    console.print(f"[bold cyan]  Decision Chain — incident:[/bold cyan] [yellow]{incident_id}[/yellow]")
    console.print(
        f"[bold cyan]  Steps:[/bold cyan] {len(chain)}  "
        f"[bold cyan]Outcome:[/bold cyan] [{outcome_color}]{outcome_global.upper()}[/{outcome_color}]"
    )
    console.print(f"[bold cyan]{'═' * 68}[/bold cyan]")
    console.print()

    for entry in chain:
        step      = entry["step"]
        component = _COMPONENT_LABELS.get(entry["component"], entry["component"])
        decision  = entry["decision"]
        reasoning = entry.get("reasoning", "")
        evidence  = entry.get("evidence", {})
        conf      = entry.get("confidence")
        outcome   = entry.get("outcome", "pending")
        ts        = datetime.fromtimestamp(entry["ts"], tz=timezone.utc).strftime("%H:%M:%S")

        step_color = {
            "correct":   "green",
            "incorrect": "red",
            "pending":   "cyan",
            "unknown":   "dim",
        }.get(outcome, "cyan")

        console.print(f"  [{step_color}]Step {step:>2}[/{step_color}]  [bold]{component}[/bold]  [dim]{ts}[/dim]")
        console.print(f"          [bold]Decision:[/bold] {decision}")

        if reasoning:
            console.print(f"          [dim]Reasoning:[/dim] {reasoning}")

        if conf is not None:
            conf_color = "green" if conf >= 0.7 else "yellow" if conf >= 0.5 else "red"
            console.print(f"          [dim]Confidence:[/dim] [{conf_color}]{conf * 100:.0f}%[/{conf_color}]")

        if evidence:
            for k, v in list(evidence.items())[:4]:
                console.print(f"          [dim]  {k}:[/dim] {v}")

        console.print()

    console.print(f"[bold cyan]{'═' * 68}[/bold cyan]")
    console.print()
