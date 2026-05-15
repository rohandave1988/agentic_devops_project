#!/usr/bin/env bash
# Prepare cluster for a specific demo scenario.
# Run BEFORE the corresponding VHS tape so VHS only records the agent.
#
# Usage: ./scripts/pre_scenario.sh <scenario>
#   scenario: error-rate | latency | code-bug
#
# What it does:
#   1. Resets buggy-app to 1 replica (clean baseline)
#   2. Ensures Loki port-forward is running
#   3. Injects the fault and starts load gen
#   4. Waits 35s for Prometheus rate(30s) window to fill
set -euo pipefail

APP_URL="http://localhost:30080"
SCENARIO="${1:-}"

if [[ -z "$SCENARIO" ]]; then
    echo "Usage: $0 <error-rate|latency|code-bug>"
    exit 1
fi

echo "━━━  Pre-scenario setup: $SCENARIO  ━━━"

# 1. Reset cluster to 1 replica
echo "► Resetting buggy-app to 1 replica..."
kubectl scale deployment/buggy-app --replicas=1 -n demo
kubectl rollout status deployment/buggy-app -n demo --timeout=60s

# 2. Ensure Loki is reachable
if ! curl -sf http://localhost:3100/ready >/dev/null 2>&1; then
    echo "► Starting Loki port-forward..."
    pkill -f "kubectl port-forward svc/loki 3100" 2>/dev/null || true
    sleep 1
    kubectl port-forward svc/loki 3100:3100 -n monitoring &>/dev/null &
    sleep 2
fi

# 3. Kill any lingering agent (port 8082 used by demo recordings)
echo "► Clearing old agent processes..."
lsof -ti:8082 | xargs kill -9 2>/dev/null || true
lsof -ti:8080 | xargs kill -9 2>/dev/null || true
sleep 1

# 4. Clear any existing fault
echo "► Clearing existing faults..."
curl -s -X POST "${APP_URL}/fault/reset" >/dev/null
sleep 1

# 4. Inject scenario-specific fault + start traffic
case "$SCENARIO" in
  error-rate)
    echo "► Injecting high error rate fault..."
    curl -s -X POST "${APP_URL}/fault/errors"
    echo ""
    ;;
  latency)
    echo "► Injecting latency fault (600ms)..."
    curl -s -X POST "${APP_URL}/fault/latency" \
         -H "Content-Type: application/json" -d '{"ms":600}'
    echo ""
    ;;
  code-bug)
    echo "► Injecting type bug (TypeError)..."
    curl -s -X POST "${APP_URL}/fault/type_bug"
    echo ""
    ;;
  stats-bug)
    echo "► Injecting stats bug (IndexError in _compute_percentile)..."
    curl -s -X POST "${APP_URL}/fault/stats_bug"
    echo ""
    ;;
  *)
    echo "Unknown scenario: $SCENARIO"
    exit 1
    ;;
esac

# 5. Background traffic generator (fills rate() window)
echo "► Generating traffic to fill Prometheus rate(30s) window..."
for i in $(seq 1 5); do
    (while true; do
        curl -sf "${APP_URL}/api/data" >/dev/null 2>&1 || true
        sleep 0.4
    done) &
done
LOAD_PIDS="$!"

# 6. Wait for window
echo "► Waiting 35s for rate window to fill..."
for i in $(seq 35 -1 1); do
    printf "\r  %2ds remaining..." "$i"
    sleep 1
done
echo ""

# 7. Kill background load gen (agent will keep issuing requests through its own traffic)
pkill -f "curl.*api/data" 2>/dev/null || true

echo ""
echo "✓ Ready — start VHS recording now:"
echo "  LLM_BACKEND=claude vhs docs/demo_s${SCENARIO/error-rate/1}.tape"
echo "  LLM_BACKEND=claude vhs docs/demo_s${SCENARIO/latency/2}.tape"
echo "  LLM_BACKEND=claude vhs docs/demo_s${SCENARIO/code-bug/3}.tape"
