#!/usr/bin/env bash
# =============================================================================
# start.sh — Start all port-forwards and launch the agent
# Run this every time you want to use the system after setup.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "Starting port-forwards..."

pkill -f "port-forward.*loki"       2>/dev/null || true
pkill -f "port-forward.*prometheus" 2>/dev/null || true
pkill -f "port-forward.*grafana"    2>/dev/null || true

kubectl port-forward svc/loki 3100:3100 -n monitoring &
echo "  Loki      → http://localhost:3100"

kubectl port-forward svc/prometheus-prometheus 9090:9090 -n monitoring &
echo "  Prometheus → http://localhost:9090"

kubectl port-forward svc/prometheus-grafana 3000:80 -n monitoring &
echo "  Grafana    → http://localhost:3000  (admin / admin)"

sleep 3

echo ""
echo "Starting Python agent..."
cd "$PROJECT_DIR/agent"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "  Created .env from .env.example — edit it to set ANTHROPIC_API_KEY"
fi

python main.py
