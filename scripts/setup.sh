#!/usr/bin/env bash
# =============================================================================
# setup.sh — Full local environment bootstrap
# Run once on a fresh Mac to stand up the entire self-healing system.
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()  { echo -e "\n${GREEN}══════════════════════════════════════${NC}"; echo -e "${GREEN}STEP: $*${NC}"; echo -e "${GREEN}══════════════════════════════════════${NC}"; }

CLUSTER_NAME="devops-agent"
NAMESPACE="demo"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

step "Checking prerequisites"
check_cmd() {
  if ! command -v "$1" &>/dev/null; then error "$1 not found. Install with: $2"; fi
  info "$1 ✓"
}
check_cmd docker  "brew install --cask docker"
check_cmd kind    "brew install kind"
check_cmd kubectl "brew install kubectl"
check_cmd helm    "brew install helm"
check_cmd python3 "brew install python"

step "Creating kind cluster: $CLUSTER_NAME"
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  warn "Cluster '$CLUSTER_NAME' already exists — skipping creation"
else
  kind create cluster --name "$CLUSTER_NAME" --config "$PROJECT_DIR/k8s/base/kind-cluster.yaml"
  info "Cluster created ✓"
fi
kubectl cluster-info --context "kind-${CLUSTER_NAME}"

step "Building buggy-app Docker image"
docker build -t buggy-app:latest "$PROJECT_DIR/buggy-app/"
kind load docker-image buggy-app:latest --name "$CLUSTER_NAME"
info "Image loaded into kind ✓"

step "Deploying buggy-app to namespace: $NAMESPACE"
kubectl apply -f "$PROJECT_DIR/k8s/buggy-app/namespace.yaml"
kubectl apply -f "$PROJECT_DIR/k8s/buggy-app/deployment.yaml"
kubectl apply -f "$PROJECT_DIR/k8s/buggy-app/service.yaml"
kubectl apply -f "$PROJECT_DIR/k8s/buggy-app/hpa.yaml"
info "Waiting for buggy-app rollout..."
kubectl rollout status deployment/buggy-app -n "$NAMESPACE" --timeout=120s
info "buggy-app deployed ✓"

step "Installing kube-prometheus-stack via Helm"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring --values "$PROJECT_DIR/k8s/monitoring/prometheus-values.yaml" \
  --wait --timeout 5m
info "Prometheus + Grafana installed ✓"

step "Installing Loki stack via Helm"
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
helm upgrade --install loki grafana/loki-stack \
  --namespace monitoring --values "$PROJECT_DIR/k8s/monitoring/loki-values.yaml" \
  --wait --timeout 3m
info "Loki + Promtail installed ✓"

step "Installing Python agent dependencies"
cd "$PROJECT_DIR/agent"
python3 -m pip install -r requirements.txt
info "Python dependencies installed ✓"

step "Setup complete!"
echo ""
echo "  Grafana:    http://localhost:30300  (admin / admin)"
echo "  Prometheus: http://localhost:30090"
echo "  buggy-app:  http://localhost:30080/healthz"
echo "  Agent:      http://localhost:8080/metrics  (after starting)"
echo ""
echo "  Start agent (Claude):"
echo "    cd agent && export ANTHROPIC_API_KEY=sk-ant-... && export LLM_BACKEND=claude && python main.py"
echo ""
echo "  Start agent (Ollama — free):"
echo "    cd agent && export LLM_BACKEND=ollama && python main.py"
echo ""
