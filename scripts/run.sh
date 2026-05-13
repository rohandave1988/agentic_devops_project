#!/usr/bin/env bash
# =============================================================================
# run.sh — Start the self-healing system
#
# Run this every time after infrastructure is already installed.
# Handles port-forwards, health checks, and agent startup.
#
# Usage:
#   chmod +x scripts/run.sh
#   ./scripts/run.sh                    # normal start
#   ./scripts/run.sh --dry-run          # investigate but don't act
#   ./scripts/run.sh --human-in-loop    # pause for approval before each action
#   ./scripts/run.sh --llm claude       # override LLM backend
#   ./scripts/run.sh --scenario errors  # inject a fault scenario after startup
# =============================================================================
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()    { echo -e "  ${YELLOW}⚠${NC}  $*"; }
error()   { echo -e "  ${RED}✗${NC}  $*"; exit 1; }
step()    { echo -e "\n${CYAN}${BOLD}▸  $*${NC}"; }
banner()  { echo -e "${CYAN}${BOLD}$*${NC}"; }

# ── Flags ─────────────────────────────────────────────────────────────────────
DRY_RUN_FLAG=false
HUMAN_IN_LOOP_FLAG=false
LLM_OVERRIDE=""
SCENARIO=""
DEMO_PATCH=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)        DRY_RUN_FLAG=true ;;
    --human-in-loop)  HUMAN_IN_LOOP_FLAG=true ;;
    --llm)            shift; LLM_OVERRIDE="$1" ;;
    --llm=*)          LLM_OVERRIDE="${1#*=}" ;;
    --scenario)       shift; SCENARIO="$1" ;;
    --scenario=*)     SCENARIO="${1#*=}" ;;
    --demo-patch)     DEMO_PATCH=true; HUMAN_IN_LOOP_FLAG=true ;;
    -h|--help)
      echo "Usage: $0 [--dry-run] [--human-in-loop] [--llm ollama|claude]"
      echo "          [--scenario errors|cpu|memory|latency]"
      echo "          [--demo-patch]   # inject errors on all pods + enable HITL patch review"
      exit 0
      ;;
    *) warn "Unknown flag: $1" ;;
  esac
  shift
done

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
AGENT_DIR="$PROJECT_DIR/agent"
VENV_DIR="$AGENT_DIR/venv"
ENV_FILE="$AGENT_DIR/.env"

CLUSTER_NAME="devops-agent"
APP_URL="http://localhost:30080"
PROM_URL="http://localhost:30090"
GRAFANA_URL="http://localhost:30300"
LOKI_URL="http://localhost:3100"

# Track background PIDs for cleanup
LOKI_PF_PID=""
LOAD_GEN_PID=""

# ── Cleanup on exit ────────────────────────────────────────────────────────────
cleanup() {
  echo ""
  banner "Shutting down…"
  if [[ -n "$LOKI_PF_PID" ]] && kill -0 "$LOKI_PF_PID" 2>/dev/null; then
    kill "$LOKI_PF_PID" 2>/dev/null || true
    info "Loki port-forward stopped"
  fi
  if [[ -n "$LOAD_GEN_PID" ]] && kill -0 "$LOAD_GEN_PID" 2>/dev/null; then
    kill "$LOAD_GEN_PID" 2>/dev/null || true
    info "Load generator stopped"
  fi
  # Reset any active faults so the app is clean for the next run
  curl -s -X POST "$APP_URL/fault/reset" > /dev/null 2>&1 || true
  echo ""
  banner "Stopped. Run ./scripts/run.sh to start again."
}
trap cleanup EXIT INT TERM

# ─────────────────────────────────────────────────────────────────────────────
echo ""
banner "╔══════════════════════════════════════════════════════════╗"
banner "║    Agentic DevOps — Self-Healing System                  ║"
banner "╚══════════════════════════════════════════════════════════╝"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight checks
# ─────────────────────────────────────────────────────────────────────────────
step "Pre-flight checks"

# Docker must be running
if ! docker info &>/dev/null; then
  error "Docker is not running. Start Docker Desktop and try again."
fi
info "Docker running"

# Cluster must exist
if ! kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  error "Kind cluster '${CLUSTER_NAME}' not found. Run scripts/install.sh first."
fi
info "Kind cluster: ${CLUSTER_NAME}"

# Switch kubectl context
kubectl config use-context "kind-${CLUSTER_NAME}" > /dev/null
info "kubectl context: kind-${CLUSTER_NAME}"

# .env must exist
if [ ! -f "$ENV_FILE" ]; then
  error ".env not found at $ENV_FILE. Run scripts/install.sh first."
fi
info ".env: $ENV_FILE"

# Virtual environment must exist
if [ ! -f "$VENV_DIR/bin/activate" ]; then
  error "Python venv not found at $VENV_DIR. Run scripts/install.sh first."
fi
info "Python venv: $VENV_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# Verify pods are healthy
# ─────────────────────────────────────────────────────────────────────────────
step "Checking Kubernetes pods"

check_pod_ready() {
  local label="$1" ns="$2" friendly="$3"
  local ready
  ready=$(kubectl get pods -n "$ns" -l "$label" \
    -o jsonpath='{.items[*].status.containerStatuses[*].ready}' 2>/dev/null | tr ' ' '\n' | grep -c "true" || echo "0")
  if [[ "$ready" -ge 1 ]]; then
    info "$friendly: ${ready} pod(s) ready"
  else
    warn "$friendly: no ready pods yet (may still be starting)"
  fi
}

check_pod_ready "app=buggy-app"   "demo"       "buggy-app"
check_pod_ready "app.kubernetes.io/name=prometheus" "monitoring" "Prometheus"
check_pod_ready "app.kubernetes.io/name=grafana"    "monitoring" "Grafana"

# ─────────────────────────────────────────────────────────────────────────────
# Kill any stale agent processes (prevent "address already in use" on port 8080)
# ─────────────────────────────────────────────────────────────────────────────
step "Cleaning up stale processes"
pkill -f "python main.py" 2>/dev/null || true
lsof -ti :8080 | xargs kill -9 2>/dev/null || true
info "Stale agent processes cleared"

# ─────────────────────────────────────────────────────────────────────────────
# Start Loki port-forward (Loki has no NodePort — needs port-forward)
# ─────────────────────────────────────────────────────────────────────────────
step "Starting Loki port-forward (localhost:3100)"

# Kill any existing Loki port-forwards
pkill -f "port-forward.*loki.*3100" 2>/dev/null || true
sleep 1

kubectl port-forward svc/loki 3100:3100 -n monitoring > /dev/null 2>&1 &
LOKI_PF_PID=$!

# Wait for Loki to accept connections (up to 15s)
loki_ready=false
for i in $(seq 1 15); do
  if curl -s --connect-timeout 2 "${LOKI_URL}/ready" | grep -q "ready"; then
    loki_ready=true
    break
  fi
  sleep 1
done

if $loki_ready; then
  info "Loki port-forward active (PID: $LOKI_PF_PID)"
else
  warn "Loki not responding yet — agent will retry. Port-forward PID: $LOKI_PF_PID"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Verify HTTP services
# ─────────────────────────────────────────────────────────────────────────────
step "Verifying services"

check_http() {
  local url="$1" label="$2" warn_only="${3:-false}"
  local http_code
  http_code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url" 2>/dev/null || echo "000")
  if [[ "$http_code" =~ ^(200|301|302|401)$ ]]; then
    info "$label  →  $url"
    return 0
  else
    if $warn_only; then
      warn "$label not reachable (HTTP $http_code) — may still be starting"
    else
      error "$label not reachable at $url (HTTP $http_code). Is the cluster running?"
    fi
    return 1
  fi
}

check_http "$APP_URL/healthz"  "buggy-app"
check_http "$PROM_URL"         "Prometheus"
check_http "$GRAFANA_URL"      "Grafana"    true   # warn-only; Grafana slow to start
check_http "$LOKI_URL/ready"   "Loki"       true   # warn-only

# ─────────────────────────────────────────────────────────────────────────────
# Validate .env for required keys
# ─────────────────────────────────────────────────────────────────────────────
step "Validating configuration"

# shellcheck source=/dev/null
set -a; source "$ENV_FILE"; set +a

# Apply CLI overrides
if $DRY_RUN_FLAG;        then DRY_RUN="true";                         fi
if $HUMAN_IN_LOOP_FLAG;  then HUMAN_IN_LOOP="true";                   fi
if [[ -n "$LLM_OVERRIDE" ]]; then LLM_BACKEND="$LLM_OVERRIDE";       fi

# Check Claude key when needed
if [[ "${LLM_BACKEND:-ollama}" == "claude" ]]; then
  if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    error "LLM_BACKEND=claude but ANTHROPIC_API_KEY is not set in $ENV_FILE"
  fi
  info "LLM: Claude (${CLAUDE_MODEL:-claude-sonnet-4-6})"
else
  info "LLM: Ollama (${OLLAMA_MODEL:-mistral-nemo})"
fi

if [[ "${DRY_RUN:-false}" == "true" ]]; then
  warn "DRY_RUN=true — agent will investigate but not execute actions"
fi
if [[ "${HUMAN_IN_LOOP:-false}" == "true" ]]; then
  warn "HUMAN_IN_LOOP=true — two approval gates for patch_code:"
  warn "  Gate 1: action choice (Enter to accept AI recommendation)"
  warn "  Gate 2: diff review   (y = commit + PR, n = discard)"
fi

# ─────────────────────────────────────────────────────────────────────────────
# inject_all_pods: send N requests to cycle through all pods (round-robin)
# so every pod picks up the fault — not just the one the LB happened to hit.
# ─────────────────────────────────────────────────────────────────────────────
inject_all_pods() {
  local endpoint="$1"
  local pods
  pods=$(kubectl get pods -n "${TARGET_NAMESPACE:-demo}" -l "app=${TARGET_DEPLOYMENT:-buggy-app}" \
    --no-headers 2>/dev/null | wc -l | tr -d ' ')
  local rounds=$(( pods > 0 ? pods * 2 : 6 ))
  for _ in $(seq 1 "$rounds"); do
    curl -s -X POST "$APP_URL/$endpoint" > /dev/null
  done
}

# ─────────────────────────────────────────────────────────────────────────────
# Optional: inject a fault scenario after agent starts
# ─────────────────────────────────────────────────────────────────────────────
if $DEMO_PATCH; then
  step "Demo: patch_code HITL flow (code_bug injection in 45s)"
  echo ""
  echo -e "  ${CYAN}What to expect:${NC}"
  echo -e "  1. Code bug injected: _get_avg_response_ms() raises ZeroDivisionError on every request"
  echo -e "  2. SLO breach detected after ~30s rate window fills"
  echo -e "  3. Multi-agent investigation runs"
  echo -e "  4. ${YELLOW}Gate 1${NC}: HITL action prompt — choose patch_code (option 5)"
  echo -e "  5. CodePatchAgent reads buggy-app/main.py, finds empty-list bug, proposes fix"
  echo -e "  6. ${YELLOW}Gate 2${NC}: Diff review — inspect the patch, press y to open PR"
  echo -e "  7. gh pr create fires → PR URL printed in logs"
  echo ""
  (
    sleep 45
    echo ""
    echo -e "  ${YELLOW}⚡ Injecting code_bug fault on all pods…${NC}"
    inject_all_pods "fault/code_bug"
    echo -e "  ${GREEN}✓${NC}  Code bug active — ZeroDivisionError on every /api/data request"
    echo -e "       SLO breach visible after ~30s rate window fills"
  ) &

elif [[ -n "$SCENARIO" ]]; then
  step "Fault scenario: $SCENARIO (injecting in 20s)"
  (
    sleep 20
    echo ""
    echo -e "  ${YELLOW}⚡ Injecting fault: $SCENARIO (all pods)${NC}"
    inject_all_pods "fault/$SCENARIO"
    echo -e "  ${GREEN}✓${NC}  Fault injected on all pods — watch agent detect and remediate"
  ) &
fi

# ─────────────────────────────────────────────────────────────────────────────
# Start background load generator (Prometheus needs traffic for rate() queries)
# ─────────────────────────────────────────────────────────────────────────────
step "Starting load generator (2 req/s → /api/data)"

LOAD_GEN_SCRIPT="$SCRIPT_DIR/load_gen.sh"
if [[ -x "$LOAD_GEN_SCRIPT" ]]; then
  bash "$LOAD_GEN_SCRIPT" --rps 2 --silent &
  LOAD_GEN_PID=$!
  info "Load generator running (PID: $LOAD_GEN_PID)"
else
  warn "load_gen.sh not found or not executable — Prometheus rate() queries may show 0 until traffic exists"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Start the agent
# ─────────────────────────────────────────────────────────────────────────────
step "Starting agent"
echo ""
echo "  Dashboards:"
echo "    Grafana     → $GRAFANA_URL          (admin / admin123)"
echo "    Prometheus  → $PROM_URL"
echo "    buggy-app   → $APP_URL"
echo "    Agent metrics → http://localhost:${AGENT_METRICS_PORT:-8080}/metrics"
echo ""
echo "  Fault injection (in a second terminal):"
echo "    curl -X POST $APP_URL/fault/errors   # 5xx error rate"
echo "    curl -X POST $APP_URL/fault/cpu      # CPU spike"
echo "    curl -X POST $APP_URL/fault/memory   # memory leak"
echo "    curl -X POST $APP_URL/fault/latency  # high latency"
echo "    curl -X POST $APP_URL/fault/reset    # reset all faults"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

# Activate venv and start agent
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

cd "$AGENT_DIR"
export DRY_RUN="${DRY_RUN:-false}"
export HUMAN_IN_LOOP="${HUMAN_IN_LOOP:-false}"
export LLM_BACKEND="${LLM_BACKEND:-ollama}"
export PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:30090}"
export LOKI_URL="${LOKI_URL:-http://localhost:3100}"

python main.py
