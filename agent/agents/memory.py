"""Per-agent persistent memory across investigations.

Each agent maintains its own memory — what questions it was asked,
what it found, and what turned out to be unexpected. This persists
across incidents so agents accumulate domain knowledge over time.

An agent that has investigated 10 CPU spikes knows patterns a fresh
agent does not. Memory is recalled at the start of each investigation
and shapes what the agent looks for.
"""
import json
import sqlite3
import time
from logging_setup import get_logger

import config

_log = get_logger("agent.memory")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT    NOT NULL,
    incident_id TEXT    NOT NULL,
    question    TEXT,
    key_facts   TEXT,       -- JSON list[str]
    unexpected  TEXT,       -- JSON list[str]
    ts          REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_memory_agent_ts ON agent_memory(agent, ts);
"""


class AgentMemory:
    """Lightweight per-agent episodic memory backed by SQLite.

    Each agent owns one instance. Memory is:
      - Recalled at investigation start to inject past experience into the prompt
      - Updated after each investigation with what was found and what was unexpected
    """

    def __init__(self, agent_name: str):
        self._agent = agent_name
        self._db    = config.DB_PATH
        self._init()

    def _init(self):
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Public API ─────────────────────────────────────────────────────────────

    def recall(self, n: int = 5) -> str:
        """Return recent memory formatted as investigation context for the LLM.

        Returns empty string if no memory exists yet.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT question, key_facts, unexpected, ts
                   FROM agent_memory
                   WHERE agent = ?
                   ORDER BY ts DESC LIMIT ?""",
                (self._agent, n),
            ).fetchall()

        if not rows:
            return ""

        lines = []
        for row in rows:
            age_min  = max(1, int((time.time() - row["ts"]) / 60))
            facts    = json.loads(row["key_facts"]    or "[]")
            surprises = json.loads(row["unexpected"]  or "[]")
            q         = (row["question"] or "")[:80]
            line      = f"  [{age_min}m ago] {q}"
            if facts:
                line += f"\n    → {'; '.join(facts[:3])}"
            if surprises:
                line += f"\n    ⚑ unexpected: {'; '.join(surprises[:2])}"
            lines.append(line)

        return "Past investigation experience:\n" + "\n".join(lines)

    def remember(
        self,
        incident_id: str,
        question: str,
        key_facts: list[str],
        unexpected: list[str],
    ) -> None:
        """Persist what was found in this investigation."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO agent_memory
                   (agent, incident_id, question, key_facts, unexpected, ts)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    self._agent,
                    incident_id,
                    (question or "")[:200],
                    json.dumps(key_facts[:10]),
                    json.dumps(unexpected[:5]),
                    time.time(),
                ),
            )
            # Keep last 100 entries per agent
            conn.execute(
                """DELETE FROM agent_memory
                   WHERE agent = ? AND id NOT IN (
                       SELECT id FROM agent_memory WHERE agent = ?
                       ORDER BY ts DESC LIMIT 100
                   )""",
                (self._agent, self._agent),
            )

        _log.debug(
            "memory updated",
            extra={"agent": self._agent, "incident_id": incident_id, "facts": len(key_facts)},
        )
