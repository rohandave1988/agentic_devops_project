#!/usr/bin/env bash
# =============================================================================
# install.sh — One-time infrastructure bootstrap
#
# Run this once on a fresh machine. Safe to re-run (idempotent).
# After this completes, use scripts/run.sh every time you want to start.
#
# Usage:
#   chmod +x scripts/install.sh
#   ./scripts/install.sh
#   ./scripts/install.sh --skip-cluster     # re-install Python deps only
#   ./scripts/install.sh --llm claude       # configure for Claude instead of Ollama
# =============================================================================
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${GREEN}  ✓${NC}  $*"; }
warn()    { echo -e "${YELLOW}  ⚠${NC}  $*"; }
error()   { echo -e "${RED}  ✗${NC}  $*"; exit 1; }
step()    { echo -e "\n${CYAN}${BOLD}━━━  $*  ━━━${NC}"; }
substep() { echo -e "     ${BOLD}▸${NC} $*"; }

# ── Flags ─────────────────────────────────────────────────────────────────────
SKIP_CLUSTER=false
LLM_BACKEND="ollama"

for arg in "$@"; do
  case $arg in
    --skip-cluster) SKIP_CLUSTER=true ;;
    --llm)          shift; LLM_BACKEND="${1:-ollama}" ;;
    --llm=*)        LLM_BACKEND="${arg#*=}" ;;
    -h|--help)
      echo "Usage: $0 [--skip-cluster] [--llm ollama|claude]"
      exit 0
      ;;
  esac
done

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
AGENT_DIR="$PROJECT_DIR/agent"
VENV_DIR="$AGENT_DIR/venv"

CLUSTER_NAME="devops-agent"
NAMESPACE="demo"

echo ""
echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}║    Agentic DevOps — Infrastructure Bootstrap             ║${NC}"
echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Check prerequisites
# ─────────────────────────────────────────────────────────────────────────────
step "Step 1 / 8 — Checking prerequisites"

check_cmd() {
  local cmd="$1" install_hint="$2"
  if ! command -v "$cmd" &>/dev/null; then
    error "$cmd not found.  Install with: $install_hint"
  fi
  info "$cmd $(command -v "$cmd")"
}

check_cmd docker   "brew install --cask docker"
check_cmd kind     "brew install kind"
check_cmd kubectl  "brew install kubectl"
check_cmd helm     "brew install helm"
check_cmd python3  "brew install python"

# Docker must actually be running (not just installed)
if ! docker info &>/dev/null; then
  error "Docker daemon is not running. Start Docker Desktop, then re-run this script."
fi
info "Docker daemon running"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Kind cluster
# ─────────────────────────────────────────────────────────────────────────────
step "Step 2 / 8 — Kubernetes cluster (kind)"

if $SKIP_CLUSTER; then
  warn "--skip-cluster passed — skipping cluster creation"
else
  if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    warn "Cluster '${CLUSTER_NAME}' already exists — skipping creation"
  else
    substep "Creating 3-node kind cluster: $CLUSTER_NAME"
    kind create cluster \
      --name "$CLUSTER_NAME" \
      --config "$PROJECT_DIR/k8s/base/kind-cluster.yaml"
    info "Cluster created"
  fi

  kubectl cluster-info --context "kind-${CLUSTER_NAME}" > /dev/null
  info "kubectl context: kind-${CLUSTER_NAME}"
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Build and load buggy-app
# ─────────────────────────────────────────────────────────────────────────────
step "Step 3 / 8 — Build buggy-app Docker image"

if $SKIP_CLUSTER; then
  warn "--skip-cluster — skipping Docker build"
else
  substep "Building buggy-app:latest"
  docker build -t buggy-app:latest "$PROJECT_DIR/buggy-app/" --quiet
  substep "Loading image into kind cluster"
  kind load docker-image buggy-app:latest --name "$CLUSTER_NAME"
  info "buggy-app image loaded into kind"
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Deploy buggy-app
# ─────────────────────────────────────────────────────────────────────────────
step "Step 4 / 8 — Deploy buggy-app (namespace: $NAMESPACE)"

if $SKIP_CLUSTER; then
  warn "--skip-cluster — skipping deployment"
else
  kubectl apply -f "$PROJECT_DIR/k8s/buggy-app/namespace.yaml"
  kubectl apply -f "$PROJECT_DIR/k8s/buggy-app/deployment.yaml"
  kubectl apply -f "$PROJECT_DIR/k8s/buggy-app/service.yaml"
  kubectl apply -f "$PROJECT_DIR/k8s/buggy-app/hpa.yaml"

  substep "Waiting for rollout…"
  kubectl rollout status deployment/buggy-app -n "$NAMESPACE" --timeout=120s
  info "buggy-app deployed and ready"
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Prometheus + Grafana
# ─────────────────────────────────────────────────────────────────────────────
step "Step 5 / 8 — Prometheus + Grafana (kube-prometheus-stack)"

if $SKIP_CLUSTER; then
  warn "--skip-cluster — skipping Helm installs"
else
  substep "Adding Helm repos"
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
  helm repo add grafana              https://grafana.github.io/helm-charts              2>/dev/null || true
  helm repo update > /dev/null

  kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

  substep "Installing kube-prometheus-stack (this takes ~3 min on first run)…"
  helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
    --namespace monitoring \
    --values "$PROJECT_DIR/k8s/monitoring/prometheus-values.yaml" \
    --wait --timeout 6m
  info "Prometheus + Grafana installed (NodePort: 30090 / 30300)"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Loki + Promtail
# ─────────────────────────────────────────────────────────────────────────────
  step "Step 6 / 8 — Loki + Promtail"
  substep "Installing loki-stack…"
  helm upgrade --install loki grafana/loki-stack \
    --namespace monitoring \
    --values "$PROJECT_DIR/k8s/monitoring/loki-values.yaml" \
    --wait --timeout 4m
  info "Loki + Promtail installed"
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — Python virtual environment + dependencies
# ─────────────────────────────────────────────────────────────────────────────
step "Step 7 / 8 — Python environment"

substep "Creating virtual environment: $VENV_DIR"
python3 -m venv "$VENV_DIR"
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

substep "Installing Python dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r "$AGENT_DIR/requirements.txt"
info "Python dependencies installed"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 8 — Agent .env configuration
# ─────────────────────────────────────────────────────────────────────────────
step "Step 8 / 8 — Agent configuration (.env)"

ENV_FILE="$AGENT_DIR/.env"
ENV_EXAMPLE="$AGENT_DIR/.env.example"

if [ -f "$ENV_FILE" ]; then
  warn ".env already exists — not overwriting. Edit manually if needed."
else
  substep "Creating $ENV_FILE from template"
  cat > "$ENV_FILE" << EOF
# ── LLM Backend ──────────────────────────────────────────────────────────────
LLM_BACKEND=${LLM_BACKEND}

# Ollama (free, local — no API key needed)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=mistral-nemo

# Claude (best results — set your key below)
# ANTHROPIC_API_KEY=sk-ant-...
# CLAUDE_MODEL=claude-sonnet-4-6

# ── Services ─────────────────────────────────────────────────────────────────
# Prometheus + Grafana are on NodePort (no port-forward needed)
PROMETHEUS_URL=http://localhost:30090
# Loki needs a port-forward — scripts/run.sh handles this automatically
LOKI_URL=http://localhost:3100

# ── Kubernetes ───────────────────────────────────────────────────────────────
KUBE_CONTEXT=kind-devops-agent
TARGET_NAMESPACE=demo
TARGET_DEPLOYMENT=buggy-app

# ── SLO Thresholds ───────────────────────────────────────────────────────────
SLO_ERROR_RATE_THRESHOLD=0.01
SLO_4XX_RATE_THRESHOLD=0.05
SLO_LATENCY_P99_MS=200
SLO_LATENCY_P50_MS=50
SLO_CPU_THRESHOLD=0.80
SLO_CPU_THROTTLE_THRESHOLD=0.25
SLO_MEMORY_THRESHOLD=0.85
SLO_OOM_KILLS_THRESHOLD=0
SLO_ACTIVE_REQUESTS_MAX=50

# ── Agent Behaviour ───────────────────────────────────────────────────────────
AGENT_POLL_INTERVAL_SEC=10
COOLDOWN_PERIOD_SEC=120
DRY_RUN=false
MIN_REPLICAS=1
MAX_REPLICAS=6
ROLLBACK_MIN_CONFIDENCE=0.6

# ── Human-in-the-Loop ────────────────────────────────────────────────────────
HUMAN_IN_LOOP=false
HUMAN_REVIEW_TIMEOUT_SEC=60

# Slack HITL (optional — only used when HUMAN_IN_LOOP=true)
# SLACK_BOT_TOKEN=xoxb-...
# SLACK_APP_TOKEN=xapp-...
# SLACK_CHANNEL=#devops-alerts

# ── Observability ─────────────────────────────────────────────────────────────
AGENT_METRICS_PORT=8080
VERIFY_DELAY_SEC=90
LOG_FORMAT=text
LOG_LEVEL=INFO

# Langfuse LLM tracing (optional)
# LANGFUSE_ENABLED=true
# LANGFUSE_PUBLIC_KEY=pk-lf-...
# LANGFUSE_SECRET_KEY=sk-lf-...
# LANGFUSE_HOST=http://localhost:3000

# OpenTelemetry → Jaeger (optional)
# OTLP_ENDPOINT=localhost:4317

# ── State ─────────────────────────────────────────────────────────────────────
DB_PATH=/tmp/agent.db
INCIDENT_HISTORY_LIMIT=50
ESCALATION_AFTER_FAILURES=2
# ESCALATION_WEBHOOK_URL=https://hooks.slack.com/...
EOF
  info ".env created"
fi

if [ "$LLM_BACKEND" = "claude" ]; then
  echo ""
  warn "LLM_BACKEND=claude selected."
  warn "Open agent/.env and set ANTHROPIC_API_KEY=sk-ant-..."
fi

# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────
if ! $SKIP_CLUSTER; then
  echo ""
  step "Health verification"

  check_http() {
    local url="$1" label="$2"
    local status
    if status=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url" 2>/dev/null); then
      if [[ "$status" =~ ^(200|301|302|401)$ ]]; then
        info "$label → $url  [HTTP $status]"
        return 0
      fi
    fi
    warn "$label not reachable yet at $url — may still be starting up"
    return 1
  }

  sleep 5
  check_http "http://localhost:30080/healthz" "buggy-app" || true
  check_http "http://localhost:30090"          "Prometheus" || true
  check_http "http://localhost:30300"          "Grafana"    || true
fi

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║    Install complete!                                      ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYAN}Services${NC}"
echo    "    buggy-app   → http://localhost:30080/healthz"
echo    "    Prometheus  → http://localhost:30090"
echo    "    Grafana     → http://localhost:30300  (admin / admin123)"
echo ""
echo -e "  ${CYAN}Next steps${NC}"
echo    "    1. Edit agent/.env  (set ANTHROPIC_API_KEY if using Claude)"
echo    "    2. Run the system:"
echo    "         ./scripts/run.sh"
echo ""
echo -e "  ${CYAN}Optional: local LLM via Ollama${NC}"
echo    "    brew install ollama"
echo    "    ollama pull mistral-nemo"
echo    "    # Then: LLM_BACKEND=ollama in agent/.env"
echo ""
