import os
from dotenv import load_dotenv

load_dotenv()

# ── LLM ───────────────────────────────────────────────────────────────────────
LLM_BACKEND   = os.environ.get("LLM_BACKEND", "ollama")      # "ollama" | "claude"
OLLAMA_URL    = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL  = os.environ.get("OLLAMA_MODEL", "mistral-nemo")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL  = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# ── Observability ─────────────────────────────────────────────────────────────
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL       = os.environ.get("LOKI_URL", "http://localhost:3100")

# ── Kubernetes ────────────────────────────────────────────────────────────────
KUBE_CONTEXT      = os.environ.get("KUBE_CONTEXT", "kind-devops-agent")
TARGET_NAMESPACE  = os.environ.get("TARGET_NAMESPACE", "demo")
TARGET_DEPLOYMENT = os.environ.get("TARGET_DEPLOYMENT", "buggy-app")

# ── SLO thresholds ────────────────────────────────────────────────────────────
SLO_ERROR_RATE = float(os.environ.get("SLO_ERROR_RATE_THRESHOLD", "0.01"))  # 1%
SLO_LATENCY_MS = float(os.environ.get("SLO_LATENCY_P99_MS", "200"))          # 200ms
SLO_CPU        = float(os.environ.get("SLO_CPU_THRESHOLD", "0.80"))           # 80%
SLO_MEMORY     = float(os.environ.get("SLO_MEMORY_THRESHOLD", "0.85"))        # 85%

# ── Agent behaviour ───────────────────────────────────────────────────────────
POLL_INTERVAL      = int(os.environ.get("AGENT_POLL_INTERVAL_SEC", "10"))
COOLDOWN_SEC       = int(os.environ.get("COOLDOWN_PERIOD_SEC", "120"))
MIN_REPLICAS       = int(os.environ.get("MIN_REPLICAS", "1"))
MAX_REPLICAS       = int(os.environ.get("MAX_REPLICAS", "6"))
ROLLBACK_MIN_CONF  = float(os.environ.get("ROLLBACK_MIN_CONFIDENCE", "0.6"))
DRY_RUN            = os.environ.get("DRY_RUN", "false").lower() == "true"

# ── Memory ────────────────────────────────────────────────────────────────────
MEMORY_FILE           = os.environ.get("MEMORY_FILE", "/tmp/agent_incidents.json")
INCIDENT_HISTORY_LIMIT = int(os.environ.get("INCIDENT_HISTORY_LIMIT", "50"))

# ── Agent self-observability ──────────────────────────────────────────────────
METRICS_PORT    = int(os.environ.get("AGENT_METRICS_PORT", "8080"))
VERIFY_DELAY_SEC = int(os.environ.get("VERIFY_DELAY_SEC", "90"))
