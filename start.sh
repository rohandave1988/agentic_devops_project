#!/usr/bin/env bash
# =============================================================================
# start.sh — Single entry point for the Agentic DevOps self-healing system
#
# First run  → runs full install (cluster + monitoring + Python deps)
# After that → starts the agent directly (port-forwards, load gen, agent)
#
# Usage:
#   ./start.sh                   # auto-detect and start
#   ./start.sh --patch-demo      # inject TypeError bug 45s after start
#   ./start.sh --scenario errors # inject 5xx fault 20s after start
#   ./start.sh --dry-run         # investigate but don't act
#   ./start.sh --reinstall       # force a full reinstall
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()   { echo -e "  ${GREEN}✓${NC}  $*"; }
warn()   { echo -e "  ${YELLOW}⚠${NC}  $*"; }
error()  { echo -e "\n  ${RED}✗  $*${NC}\n"; exit 1; }
step()   { echo -e "\n${CYAN}${BOLD}▸  $*${NC}"; }
header() { echo -e "${CYAN}${BOLD}$*${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$SCRIPT_DIR/agent"
ENV_FILE="$AGENT_DIR/.env"
VENV_DIR="$AGENT_DIR/venv"
CLUSTER_NAME="devops-agent"

REINSTALL=false
PATCH_DEMO=false
SCENARIO=""
DRY_RUN=false
HUMAN_IN_LOOP=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reinstall)      REINSTALL=true ;;
    --patch-demo)     PATCH_DEMO=true; HUMAN_IN_LOOP=true ;;
    --scenario)       shift; SCENARIO="$1" ;;
    --scenario=*)     SCENARIO="${1#*=}" ;;
    --dry-run)        DRY_RUN=true ;;
    --human-in-loop)  HUMAN_IN_LOOP=true ;;
    -h|--help)
      echo ""
      echo "  Usage: ./start.sh [options]"
      echo ""
      echo "  Options:"
      echo "    (none)              Auto-detect first run vs normal start"
      echo "    --patch-demo        Inject TypeError bug 45s after start"
      echo "    --scenario NAME     Inject fault: errors | cpu | memory | latency"
      echo "    --dry-run           Investigate but don't act"
      echo "    --human-in-loop     Pause for approval before each action"
      echo "    --reinstall         Force full reinstall (keeps .env)"
      echo ""
      exit 0
      ;;
    *) warn "Unknown flag: $1" ;;
  esac
  shift
done

echo ""
header "╔══════════════════════════════════════════════════════════╗"
header "║    Agentic DevOps — Self-Healing Kubernetes System       ║"
header "╚══════════════════════════════════════════════════════════╝"
echo ""

if ! docker info &>/dev/null; then
  error "Docker is not running. Start Docker Desktop and try again."
fi

needs_install() {
  $REINSTALL && return 0
  ! kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$" && return 0
  [ ! -f "$VENV_DIR/bin/activate" ] && return 0
  return 1
}

if needs_install; then
  echo ""
  header "  First run detected — running full install (~5 min)"
  echo ""

  echo -e "  ${BOLD}Choose LLM backend:${NC}"
  echo "    1) Ollama — local, free, no API key  (recommended for demo)"
  echo "    2) Claude — best reasoning quality   (requires ANTHROPIC_API_KEY)"
  echo ""
  read -r -p "  Enter 1 or 2 [default: 1]: " llm_choice
  llm_choice="${llm_choice:-1}"

  if [[ "$llm_choice" == "2" ]]; then
    LLM_ARG="--llm claude"
    echo ""
    echo -e "  ${YELLOW}Note:${NC} After install, open agent/.env and set ANTHROPIC_API_KEY=sk-ant-..."
  else
    LLM_ARG="--llm ollama"
    if ! command -v ollama &>/dev/null; then
      warn "Ollama not found. Install it: brew install ollama && ollama pull qwen2.5:7b"
    else
      MODEL="${OLLAMA_MODEL:-qwen2.5:7b}"
      if ! ollama list 2>/dev/null | grep -q "$MODEL"; then
        step "Pulling Ollama model: $MODEL (~4 GB)"
        ollama pull "$MODEL"
        info "$MODEL ready"
      else
        info "Ollama model $MODEL already present"
      fi
    fi
  fi

  echo ""
  # shellcheck disable=SC2086
  bash "$SCRIPT_DIR/scripts/install.sh" $LLM_ARG

  echo ""
  header "  Install complete. Starting the agent now..."
  echo ""
fi

if [ ! -f "$ENV_FILE" ]; then
  warn ".env not found — creating from template"
  cp "$AGENT_DIR/.env.example" "$ENV_FILE"
  warn "Edit agent/.env before running again (set LLM_BACKEND and any API keys)"
  exit 1
fi

# shellcheck source=/dev/null
set -a; source "$ENV_FILE"; set +a

if [[ "${LLM_BACKEND:-ollama}" == "claude" ]] && [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  error "LLM_BACKEND=claude but ANTHROPIC_API_KEY is not set.\nOpen agent/.env and add: ANTHROPIC_API_KEY=sk-ant-..."
fi

# ── Start Langfuse LLM tracing (always) ───────────────────────────────────────
LANGFUSE_COMPOSE="$SCRIPT_DIR/docker-compose.langfuse.yml"
step "Starting Langfuse LLM tracing"

if [ ! -f "$LANGFUSE_COMPOSE" ]; then
  warn "docker-compose.langfuse.yml not found — skipping Langfuse"
else
  if ! docker compose -f "$LANGFUSE_COMPOSE" ps --status running 2>/dev/null | grep -q "langfuse"; then
    docker compose -f "$LANGFUSE_COMPOSE" up -d --quiet-pull 2>/dev/null
    info "Langfuse stack started"
  else
    info "Langfuse already running"
  fi

  if grep -q "^LANGFUSE_ENABLED=false" "$ENV_FILE"; then
    sed -i '' 's/^LANGFUSE_ENABLED=false/LANGFUSE_ENABLED=true/' "$ENV_FILE"
    info "LANGFUSE_ENABLED set to true in agent/.env"
  fi

  langfuse_ready=false
  for i in $(seq 1 20); do
    if curl -s --connect-timeout 2 "http://localhost:3000/api/public/health" \
         2>/dev/null | grep -qi '"status"'; then
      langfuse_ready=true; break
    fi
    sleep 1
  done

  if $langfuse_ready; then
    info "Langfuse ready → http://localhost:3000"
  else
    warn "Langfuse not responding yet — open http://localhost:3000 in a moment"
  fi
fi

# ── Launch ────────────────────────────────────────────────────────────────────
RUN_ARGS=()
$DRY_RUN        && RUN_ARGS+=("--dry-run")
$HUMAN_IN_LOOP  && RUN_ARGS+=("--human-in-loop")
$PATCH_DEMO     && RUN_ARGS+=("--demo-patch")
[[ -n "$SCENARIO" ]] && RUN_ARGS+=("--scenario" "$SCENARIO")

echo ""
echo -e "  ${BOLD}System URLs:${NC}"
echo "    buggy-app   → http://localhost:30080"
echo "    Prometheus  → http://localhost:30090"
echo "    Grafana     → http://localhost:30300   (admin / admin123)"
echo "    Langfuse    → http://localhost:3000    (LLM call traces)"
echo ""
echo -e "  ${BOLD}Inject a fault in a separate terminal:${NC}"
echo "    curl -X POST http://localhost:30080/fault/errors    # high 5xx rate"
echo "    curl -X POST http://localhost:30080/fault/cpu       # CPU spike"
echo "    curl -X POST http://localhost:30080/fault/memory    # memory leak"
echo "    curl -X POST http://localhost:30080/fault/latency   # high latency"
echo "    curl -X POST http://localhost:30080/fault/type_bug  # TypeError (needs ALLOW_CODE_PATCHES=true)"
echo "    curl -X POST http://localhost:30080/fault/reset     # reset all faults"
echo ""

if $PATCH_DEMO; then
  echo -e "  ${YELLOW}Patch demo mode:${NC} TypeError will be injected in 45s."
  echo "  Watch for the two HITL prompts in the agent output."
  echo ""
fi

exec bash "$SCRIPT_DIR/scripts/run.sh" "${RUN_ARGS[@]+"${RUN_ARGS[@]}"}"
