"""SLOStateTracker — debounce noisy SLO violations before triggering investigation.

Mirrors Prometheus alerting-rule `for:` behavior: a violation must persist for
SLO_SUSTAINED_SEC consecutive seconds before the agent treats it as a real incident.

Without this, a single scrape spike that clears itself in the next cycle
triggers a full LLM investigation cycle — burning tokens and risking unneeded actions.
"""
import time
from dataclasses import dataclass, field

import config


@dataclass
class _ViolationState:
    first_seen: float = 0.0
    active: bool = False      # True once violation persisted >= SLO_SUSTAINED_SEC


class SLOStateTracker:
    """Per-violation-type sustained window filter."""

    def __init__(self, sustained_sec: int | None = None):
        self._sustained = sustained_sec if sustained_sec is not None else config.SLO_SUSTAINED_SEC
        self._states: dict[str, _ViolationState] = {}

    def update(self, violations: list[str]) -> list[str]:
        """Feed current poll-cycle violations. Returns those sustained long enough to act on.

        Violations that first appeared this cycle are tracked but not returned.
        Violations that cleared are removed from state entirely (hysteresis resets).
        """
        now = time.monotonic()
        current_keys = {self._key(v) for v in violations}

        # Evict states for violations that have now cleared
        for k in list(self._states):
            if k not in current_keys:
                del self._states[k]

        sustained: list[str] = []
        for v in violations:
            k = self._key(v)
            state = self._states.get(k)
            if state is None:
                self._states[k] = _ViolationState(first_seen=now, active=False)
            else:
                if not state.active and (now - state.first_seen) >= self._sustained:
                    state.active = True
                if state.active:
                    sustained.append(v)

        return sustained

    def pending(self) -> dict[str, float]:
        """Return violations seen but not yet sustained, with seconds elapsed."""
        now = time.monotonic()
        return {
            k: round(now - s.first_seen, 1)
            for k, s in self._states.items()
            if not s.active
        }

    @staticmethod
    def _key(violation: str) -> str:
        # Use the TYPE prefix (e.g. "ERROR_RATE_BREACH") so numeric value changes
        # in consecutive cycles don't reset the sustain window.
        return violation.split(":")[0].strip()
