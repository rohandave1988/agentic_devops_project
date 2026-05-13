import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

import config

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id              TEXT PRIMARY KEY,
    namespace       TEXT,
    deployment      TEXT,
    anomalies       TEXT,
    root_cause      TEXT,
    severity        TEXT,
    action_taken    TEXT,
    action_result   TEXT,
    confidence      REAL DEFAULT 0.0,
    metrics_snapshot TEXT,
    outcome_notes   TEXT DEFAULT '',
    slo_recovered   INTEGER DEFAULT 0,
    mttr_sec        INTEGER DEFAULT 0,
    started_at      REAL,
    resolved_at     REAL
);

CREATE TABLE IF NOT EXISTS metric_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    namespace       TEXT,
    deployment      TEXT,
    cpu_usage       REAL,
    memory_usage    REAL,
    error_rate      REAL,
    latency_p99_ms  REAL,
    latency_p50_ms  REAL,
    oom_kills       INTEGER,
    pod_restarts    INTEGER,
    ready_replicas  INTEGER,
    desired_replicas INTEGER
);

CREATE INDEX IF NOT EXISTS idx_incidents_started ON incidents(started_at);
CREATE INDEX IF NOT EXISTS idx_metric_history_ts ON metric_history(ts, namespace, deployment);
"""


@dataclass
class Incident:
    id: str
    timestamp: int
    anomalies: list[str]
    root_cause: str
    severity: str
    action_taken: str
    action_result: str
    metrics_snapshot: dict[str, Any]
    confidence: float = 0.0
    outcome_notes: str = ""
    slo_recovered: bool = False
    mttr_sec: int = 0


class Store:
    """SQLite-backed incident log + metric history. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._db   = config.DB_PATH
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ── Incident API ──────────────────────────────────────────────────────────

    def record(
        self,
        anomalies: list[str],
        root_cause: str,
        severity: str,
        action: str,
        result: str,
        snapshot: dict[str, Any],
        confidence: float = 0.0,
        incident_id: str = "",
    ) -> Incident:
        inc = Incident(
            id=incident_id or f"inc-{time.time_ns()}",
            timestamp=int(time.time()),
            anomalies=anomalies,
            root_cause=root_cause,
            severity=severity,
            action_taken=action,
            action_result=result,
            metrics_snapshot=snapshot,
            confidence=confidence,
        )
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO incidents
                   (id, namespace, deployment, anomalies, root_cause, severity,
                    action_taken, action_result, confidence, metrics_snapshot, started_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    inc.id, config.TARGET_NAMESPACE, config.TARGET_DEPLOYMENT,
                    json.dumps(anomalies), root_cause, severity,
                    action, result, confidence,
                    json.dumps(snapshot), inc.timestamp,
                ),
            )
            self._trim(conn)
        logger.info(f"incident recorded: id={inc.id} root_cause={root_cause} action={action}")
        return inc

    def update_outcome(self, incident_id: str, recovered: bool, mttr_sec: int):
        now = time.time()
        with self._lock, self._conn() as conn:
            conn.execute(
                """UPDATE incidents
                   SET slo_recovered=?, mttr_sec=?, resolved_at=?
                   WHERE id=?""",
                (int(recovered), mttr_sec, now, incident_id),
            )
        logger.info(
            f"incident outcome updated: id={incident_id} "
            f"slo_recovered={recovered} mttr_sec={mttr_sec}"
        )

    def get_recent(self, n: int = 5) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?", (n,)
            ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    def count_unresolved_recent(self, window_minutes: int = 30) -> int:
        """Count incidents in the last N minutes where SLOs did not recover."""
        since = time.time() - window_minutes * 60
        with self._lock, self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) FROM incidents
                   WHERE started_at >= ? AND slo_recovered=0 AND resolved_at IS NOT NULL""",
                (since,),
            ).fetchone()
        return row[0] if row else 0

    # ── Metric history API ────────────────────────────────────────────────────

    def insert_metric_snapshot(self, metrics) -> None:
        """Record one metric snapshot per agent cycle for trend analysis."""
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO metric_history
                   (ts, namespace, deployment, cpu_usage, memory_usage,
                    error_rate, latency_p99_ms, latency_p50_ms, oom_kills,
                    pod_restarts, ready_replicas, desired_replicas)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(),
                    config.TARGET_NAMESPACE, config.TARGET_DEPLOYMENT,
                    metrics.cpu_usage, metrics.memory_usage,
                    metrics.error_rate, metrics.latency_p99_ms,
                    getattr(metrics, "latency_p50_ms", 0.0),
                    getattr(metrics, "oom_kills", 0),
                    metrics.pod_restarts, metrics.ready_replicas,
                    metrics.desired_replicas,
                ),
            )
            # Keep 24h of history
            conn.execute(
                "DELETE FROM metric_history WHERE ts < ?",
                (time.time() - 86400,),
            )

    def get_metric_trend(self, minutes: int = 30) -> list[dict]:
        """Return metric snapshots for the last N minutes — for LLM context."""
        since = time.time() - minutes * 60
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT ts, cpu_usage, memory_usage, error_rate,
                          latency_p99_ms, latency_p50_ms, oom_kills,
                          pod_restarts, ready_replicas, desired_replicas
                   FROM metric_history
                   WHERE ts >= ? AND namespace=? AND deployment=?
                   ORDER BY ts""",
                (since, config.TARGET_NAMESPACE, config.TARGET_DEPLOYMENT),
            ).fetchall()
        return [dict(r) for r in rows]

    def _trim(self, conn: sqlite3.Connection):
        conn.execute(
            """DELETE FROM incidents WHERE id NOT IN (
               SELECT id FROM incidents ORDER BY started_at DESC LIMIT ?)""",
            (config.INCIDENT_HISTORY_LIMIT,),
        )


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("anomalies", "metrics_snapshot"):
        if isinstance(d.get(key), str):
            d[key] = json.loads(d[key])
    d["slo_recovered"] = bool(d.get("slo_recovered"))
    return d
