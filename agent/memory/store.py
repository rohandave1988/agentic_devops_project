import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import config

logger = logging.getLogger(__name__)


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
    outcome_notes: str = ""
    # Populated ~90s after action by the verifier thread
    slo_recovered: bool = False
    mttr_sec: int = 0


class Store:
    """File-backed incident log with mutex protection for concurrent access."""

    def __init__(self):
        self._lock = threading.Lock()
        self._path = config.MEMORY_FILE
        try:
            with open(self._path) as f:
                json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._write([])

    def record(
        self,
        anomalies: list[str],
        root_cause: str,
        severity: str,
        action: str,
        result: str,
        snapshot: dict[str, Any],
    ) -> Incident:
        inc = Incident(
            id=f"inc-{time.time_ns()}",
            timestamp=int(time.time()),
            anomalies=anomalies,
            root_cause=root_cause,
            severity=severity,
            action_taken=action,
            action_result=result,
            metrics_snapshot=snapshot,
        )
        with self._lock:
            all_ = self._load()
            all_.append(inc)
            if len(all_) > config.INCIDENT_HISTORY_LIMIT:
                all_ = all_[-config.INCIDENT_HISTORY_LIMIT:]
            self._write(all_)
        logger.info(f"incident recorded: id={inc.id} root_cause={root_cause} action={action}")
        return inc

    def get_recent(self, n: int) -> list[dict]:
        with self._lock:
            all_ = self._load()
        return [asdict(inc) for inc in all_[-n:]]

    def update_outcome(self, incident_id: str, recovered: bool, mttr_sec: int):
        with self._lock:
            all_ = self._load()
            for inc in all_:
                if inc.id == incident_id:
                    inc.slo_recovered = recovered
                    inc.mttr_sec = mttr_sec
                    break
            self._write(all_)
        logger.info(
            f"incident outcome updated: id={incident_id} "
            f"slo_recovered={recovered} mttr_sec={mttr_sec}"
        )

    def _load(self) -> list[Incident]:
        try:
            with open(self._path) as f:
                return [Incident(**d) for d in json.load(f)]
        except Exception:
            return []

    def _write(self, incidents: list[Incident]):
        try:
            with open(self._path, "w") as f:
                json.dump([asdict(i) for i in incidents], f, indent=2)
        except OSError as e:
            logger.error(f"failed to persist incident store: {e}")
