"""Core data types shared by all agents.

IncidentContext  — everything an agent needs to investigate one incident.
Finding          — structured output from a specialist agent.
Diagnosis        — final orchestrator output consumed by the planner and executor.
"""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IncidentContext:
    incident_id: str
    violations:  list[str]
    metrics:     Any        # perception.prometheus.ClusterMetrics
    loki:        Any        # perception.loki.LokiClient
    store:       Any        # memory.store.Store


@dataclass
class Finding:
    """Structured output from a specialist agent after LLM-driven investigation.

    Richer than a plain string: the orchestrator can reason about confidence,
    act on key_facts directly, and follow up on unexpected observations without
    waiting to read them out of prose.
    """
    agent:      str          # "metrics" | "logs" | "history"
    analysis:   str          # Full natural-language answer to the orchestrator's question
    confidence: float        # 0.0–1.0: how certain the specialist is
    key_facts:  list[str]    # Concrete extracted facts (metric values, error types, counts)
    unexpected: list[str]    # Observations found that weren't asked about — proactive signal


@dataclass
class Diagnosis:
    """Orchestrator's final verdict, consumed by DecisionEngine and Executor."""
    root_cause:        str
    severity:          str           # critical | high | medium | low
    suggested_actions: list[str]     # priority-ordered canonical action names
    confidence:        float         # 0.0–1.0
    anomalies:         list[str]
    reasoning:         str = ""
