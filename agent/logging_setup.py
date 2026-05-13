"""Structured logging with OpenTelemetry trace correlation.

Every log record carries trace_id and span_id from the currently active OTel
span. This means you can open any log line in Jaeger and jump directly to the
trace that produced it.

JSON format (LOG_FORMAT=json) — use in production / log aggregators:
  {
    "ts":       "2026-05-02T10:23:45Z",
    "level":    "INFO",
    "agent":    "orchestrator",
    "msg":      "investigation started",
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    "span_id":  "00f067aa0ba902b7",
    "incident_id": "run-17431234567"
  }

Text format (LOG_FORMAT=text, default) — human-readable in development:
  ts=2026-05-02T10:23:45Z level=INFO agent=orchestrator
  trace=4bf9... span=00f0... msg=investigation started incident_id=run-1234

Usage:
    from logging_setup import get_logger
    log = get_logger("orchestrator")
    log.info("investigation started", extra={"incident_id": id, "violations": 3})
"""
import json
import logging
import sys
from datetime import datetime, timezone

import config

# ── Fields to suppress from extra= forwarding ─────────────────────────────────

_SKIP = frozenset({
    "name", "msg", "args", "created", "relativeCreated",
    "thread", "threadName", "processName", "process",
    "pathname", "filename", "module", "lineno", "funcName",
    "stack_info", "exc_info", "exc_text", "levelno", "msecs",
    "trace_id", "span_id",   # handled explicitly by formatters
    # asyncio / stdlib noise never useful in output
    "taskName", "levelname",
})

# ── ANSI colour palette ───────────────────────────────────────────────────────

_C = {
    "reset":   "\033[0m",
    "bold":    "\033[1m",
    "dim":     "\033[2m",
    "red":     "\033[31m",
    "green":   "\033[32m",
    "yellow":  "\033[33m",
    "cyan":    "\033[36m",
    "magenta": "\033[35m",
    "white":   "\033[97m",
    "grey":    "\033[90m",
}

_LEVEL_COLOR = {
    "DEBUG":    _C["grey"],
    "INFO":     _C["green"],
    "WARNING":  _C["yellow"],
    "ERROR":    _C["red"],
    "CRITICAL": _C["red"] + _C["bold"],
}

# Per-agent accent colours for quick visual scanning
_AGENT_COLOR = {
    "orchestrator":  _C["cyan"],
    "metrics":       _C["magenta"],
    "logs":          _C["yellow"],
    "history":       _C["green"],
    "decision":      _C["white"],
    "main":          _C["white"],
    "store":         _C["grey"],
    "code-patch-agent": _C["magenta"],
    "git-ops":       _C["cyan"],
    "build-deploy":  _C["cyan"],
}


# ── OTel trace context filter ─────────────────────────────────────────────────

class _OTelContextFilter(logging.Filter):
    """Injects trace_id and span_id from the active OTel span into every record.

    Safe before setup_tracing() is called — OTel's NoOp span returns an invalid
    context, producing empty strings rather than crashing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from opentelemetry import trace as _t
            ctx = _t.get_current_span().get_span_context()
            if ctx.is_valid:
                record.trace_id = format(ctx.trace_id, "032x")
                record.span_id  = format(ctx.span_id,  "016x")
            else:
                record.trace_id = ""
                record.span_id  = ""
        except Exception:
            record.trace_id = ""
            record.span_id  = ""
        return True


# ── Formatters ────────────────────────────────────────────────────────────────

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts  = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc: dict = {
            "ts":       ts,
            "level":    record.levelname,
            "agent":    getattr(record, "agent", record.name.split(".")[-1]),
            "msg":      record.getMessage(),
            "trace_id": getattr(record, "trace_id", ""),
            "span_id":  getattr(record, "span_id",  ""),
        }
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k not in _SKIP and not k.startswith("_") and k not in doc:
                doc[k] = v
        return json.dumps(doc, default=str)


class _TextFormatter(logging.Formatter):
    """Verbose coloured format — bold message, all fields shown prominently.

    Layout:
      ┌ header line:  HH:MM:SS  LEVEL    agent-name  │  MESSAGE TEXT
      └ detail line:  (indented) key=value  key=value  trace=abc123…
    """

    def format(self, record: logging.LogRecord) -> str:
        ts    = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        agent = getattr(record, "agent", record.name.split(".")[-1])
        level = record.levelname
        msg   = record.getMessage()

        lc  = _LEVEL_COLOR.get(level, "")
        ac  = _AGENT_COLOR.get(agent, _C["grey"])
        rst = _C["reset"]
        dim = _C["dim"]
        bld = _C["bold"]

        # ── Header line ───────────────────────────────────────────────────────
        header = (
            f"{dim}{ts}{rst}  "
            f"{lc}{bld}{level:<8}{rst}  "
            f"{ac}{bld}{agent:<20}{rst}  "
            f"{bld}{msg}{rst}"
        )

        # ── Detail line — all user-supplied extra fields ───────────────────────
        fields = {}
        for k, v in record.__dict__.items():
            if k not in _SKIP and not k.startswith("_") and k not in {"agent", "message"}:
                fields[k] = v

        tid = getattr(record, "trace_id", "")
        if tid:
            fields["trace"] = tid[:16]

        if fields:
            pairs = "  ".join(
                f"{dim}{k}{rst}={lc}{v}{rst}" for k, v in fields.items()
            )
            detail = f"         {' ' * 28}{pairs}"
            line   = header + "\n" + detail
        else:
            line = header

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


# ── Public API ─────────────────────────────────────────────────────────────────

def setup(fmt: str | None = None, level: str | None = None) -> None:
    """Configure root logger. Call once at startup before setup_tracing()."""
    fmt   = (fmt   or config.LOG_FORMAT).lower()
    level = (level or config.LOG_LEVEL).upper()

    formatter = _JsonFormatter() if fmt == "json" else _TextFormatter()
    handler   = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(_OTelContextFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    for lib in ("urllib3", "requests", "anthropic", "httpx", "httpcore", "opentelemetry"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def get_logger(agent_name: str) -> logging.LoggerAdapter:
    """Return a LoggerAdapter that injects agent= into every record."""
    logger = logging.getLogger(f"agents.{agent_name}")
    return logging.LoggerAdapter(logger, {"agent": agent_name})
