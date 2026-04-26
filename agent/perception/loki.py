import re
import time
import logging

import requests

import config

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")
_MAX_LINES = 40


class LokiClient:
    def __init__(self):
        self._base = config.LOKI_URL
        self._session = requests.Session()

    def query_recent_logs(self, lookback_sec: int = 120) -> list[str]:
        now_ns  = int(time.time() * 1e9)
        start_ns = now_ns - lookback_sec * int(1e9)
        logql   = f'{{namespace="{config.TARGET_NAMESPACE}", app="{config.TARGET_DEPLOYMENT}"}}'

        try:
            resp = self._session.get(
                f"{self._base}/loki/api/v1/query_range",
                params={
                    "query": logql,
                    "start": start_ns,
                    "end":   now_ns,
                    "limit": _MAX_LINES,
                    "direction": "forward",
                },
                timeout=10,
            )
            resp.raise_for_status()
            lines: list[str] = []
            for stream in resp.json().get("data", {}).get("result", []):
                for _, line in stream.get("values", []):
                    clean = _sanitize(line)
                    if clean:
                        lines.append(clean)
            # deduplicate while preserving order
            seen: set[str] = set()
            deduped = [l for l in lines if not (l in seen or seen.add(l))]  # type: ignore[func-returns-value]
            return deduped[-_MAX_LINES:]
        except Exception as e:
            logger.debug(f"loki query failed: {e}")
            return []


def format_for_llm(lines: list[str]) -> str:
    return "\n".join(lines) if lines else "(no recent logs available)"


def _sanitize(s: str) -> str:
    s = _ANSI_RE.sub("", s)
    s = "".join(c for c in s if not (ord(c) < 32 and c not in "\t\n"))
    return s.strip()
