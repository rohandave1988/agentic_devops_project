import os
from dotenv import load_dotenv

load_dotenv()

# ── LLM ───────────────────────────────────────────────────────────────────────
LLM_BACKEND   = os.environ.get("LLM_BACKEND", "ollama")      # "ollama" | "claude"
OLLAMA_URL    = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL  = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL  = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# ── Observability ─────────────────────────────────────────────────────────────
PROMETHEUS_URL  = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL        = os.environ.get("LOKI_URL", "http://localhost:3100")
RATE_WINDOW     = os.environ.get("PROMETHEUS_RATE_WINDOW", "2m")

# ── Kubernetes ────────────────────────────────────────────────────────────────
KUBE_CONTEXT      = os.environ.get("KUBE_CONTEXT", "kind-devops-agent")
TARGET_NAMESPACE  = os.environ.get("TARGET_NAMESPACE", "demo")
TARGET_DEPLOYMENT = os.environ.get("TARGET_DEPLOYMENT", "buggy-app")

# ── SLO thresholds ────────────────────────────────────────────────────────────
SLO_ERROR_RATE      = float(os.environ.get("SLO_ERROR_RATE_THRESHOLD", "0.01"))   # 5xx rate 1%
SLO_4XX_RATE        = float(os.environ.get("SLO_4XX_RATE_THRESHOLD", "0.05"))     # 4xx rate 5%
SLO_LATENCY_MS      = float(os.environ.get("SLO_LATENCY_P99_MS", "200"))          # P99 200ms
SLO_LATENCY_P50     = float(os.environ.get("SLO_LATENCY_P50_MS", "50"))           # P50 50ms
SLO_CPU             = float(os.environ.get("SLO_CPU_THRESHOLD", "0.80"))           # 80%
SLO_CPU_THROTTLE    = float(os.environ.get("SLO_CPU_THROTTLE_THRESHOLD", "0.25")) # 25% throttled
SLO_MEMORY          = float(os.environ.get("SLO_MEMORY_THRESHOLD", "0.60"))        # 60% — gives ~20s window before OOM at 512Mi limit
SLO_OOM_KILLS       = int(os.environ.get("SLO_OOM_KILLS_THRESHOLD", "0"))          # any OOM = breach
SLO_ACTIVE_REQUESTS = int(os.environ.get("SLO_ACTIVE_REQUESTS_MAX", "50"))         # request saturation
SLO_CHECK_P50       = os.environ.get("SLO_CHECK_P50", "false").lower() == "true"  # opt-in

# ── Agent behaviour ───────────────────────────────────────────────────────────
POLL_INTERVAL      = int(os.environ.get("AGENT_POLL_INTERVAL_SEC", "10"))
COOLDOWN_SEC       = int(os.environ.get("COOLDOWN_PERIOD_SEC", "120"))
MIN_REPLICAS       = int(os.environ.get("MIN_REPLICAS", "1"))
MAX_REPLICAS       = int(os.environ.get("MAX_REPLICAS", "6"))
ROLLBACK_MIN_CONF  = float(os.environ.get("ROLLBACK_MIN_CONFIDENCE", "0.6"))
DRY_RUN            = os.environ.get("DRY_RUN", "false").lower() == "true"

# ── State (SQLite) ────────────────────────────────────────────────────────────
DB_PATH                = os.environ.get("DB_PATH", "/tmp/agent.db")
INCIDENT_HISTORY_LIMIT = int(os.environ.get("INCIDENT_HISTORY_LIMIT", "50"))

# ── Escalation ────────────────────────────────────────────────────────────────
# Set ESCALATION_WEBHOOK_URL to a Slack incoming webhook or any HTTP endpoint.
# Agent fires it when ESCALATION_AFTER_FAILURES consecutive incidents go unresolved.
ESCALATION_WEBHOOK_URL  = os.environ.get("ESCALATION_WEBHOOK_URL", "")
ESCALATION_FAILURES     = int(os.environ.get("ESCALATION_AFTER_FAILURES", "2"))

# ── Agent self-observability ──────────────────────────────────────────────────
METRICS_PORT     = int(os.environ.get("AGENT_METRICS_PORT", "8080"))
VERIFY_DELAY_SEC = int(os.environ.get("VERIFY_DELAY_SEC", "90"))
# Max total seconds to poll for recovery before giving up (verification polling).
VERIFY_MAX_WAIT_SEC = int(os.environ.get("VERIFY_MAX_WAIT_SEC", "300"))
# Seconds a violation must persist before triggering investigation (SLO hysteresis).
SLO_SUSTAINED_SEC = int(os.environ.get("SLO_SUSTAINED_SEC", "20"))
# Minimum average specialist confidence required for evidence gate to pass (0.0–1.0).
MIN_SPECIALIST_CONFIDENCE = float(os.environ.get("MIN_SPECIALIST_CONFIDENCE", "0.35"))

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_FORMAT = os.environ.get("LOG_FORMAT", "text")   # "text" | "json"
LOG_LEVEL  = os.environ.get("LOG_LEVEL",  "INFO")   # DEBUG | INFO | WARNING | ERROR

# ── Human-in-the-Loop ─────────────────────────────────────────────────────────
# When HUMAN_IN_LOOP=true, the agent pauses after diagnosis and waits for
# a human to approve or override the suggested action in the terminal.
# Auto-approves after HUMAN_REVIEW_TIMEOUT_SEC seconds.
HUMAN_IN_LOOP        = os.environ.get("HUMAN_IN_LOOP", "false").lower() == "true"
HUMAN_REVIEW_TIMEOUT = int(os.environ.get("HUMAN_REVIEW_TIMEOUT_SEC", "60"))

# ── Langfuse observability ────────────────────────────────────────────────────
LANGFUSE_ENABLED    = os.environ.get("LANGFUSE_ENABLED", "false").lower() == "true"
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST       = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")

# ── OpenTelemetry tracing ─────────────────────────────────────────────────────
# OTLP_ENDPOINT: gRPC endpoint for Jaeger / any OTLP collector.
#   Local Jaeger: run `docker run -p 16686:16686 -p 4317:4317 jaegertracing/all-in-one`
#   then set OTLP_ENDPOINT=localhost:4317
# TRACING_CONSOLE: set true to print spans to stdout (dev only, noisy)
OTLP_ENDPOINT   = os.environ.get("OTLP_ENDPOINT",    "")
TRACING_CONSOLE = os.environ.get("TRACING_CONSOLE",  "false").lower() == "true"
SERVICE_NAME    = os.environ.get("SERVICE_NAME",      "devops-agent")
SERVICE_VERSION = os.environ.get("SERVICE_VERSION",   "1.0.0")

# ── Code patch + PR + deploy ──────────────────────────────────────────────────
# APP_SOURCE_DIR: path to the application source (where Dockerfile lives).
#   Resolved relative to this config.py file's parent (the agent/ directory).
_agent_dir = os.path.dirname(os.path.abspath(__file__))
APP_SOURCE_DIR      = os.environ.get(
    "APP_SOURCE_DIR",
    os.path.join(_agent_dir, "..", "buggy-app"),
)
# Allow the agent to generate and commit code patches (opt-in).
ALLOW_CODE_PATCHES  = os.environ.get("ALLOW_CODE_PATCHES", "false").lower() == "true"
# Auto-build and deploy the patch after opening the PR (opt-in, requires ALLOW_CODE_PATCHES).
AUTO_DEPLOY_PATCH   = os.environ.get("AUTO_DEPLOY_PATCH",  "false").lower() == "true"
# Minimum LLM confidence required before a patch is committed.
PATCH_MIN_CONF      = float(os.environ.get("PATCH_MIN_CONFIDENCE", "0.75"))
# Git branch prefix for agent-generated patches.
PATCH_BRANCH_PREFIX = os.environ.get("PATCH_BRANCH_PREFIX", "agent-fix")
# Default branch PRs target (main or master).
DEFAULT_BRANCH      = os.environ.get("DEFAULT_BRANCH", "main")
# Kind cluster name — used by build_deploy.py when loading images.
KIND_CLUSTER_NAME   = os.environ.get("KIND_CLUSTER_NAME", "devops-agent")
