#!/usr/bin/env bash
# =============================================================================
# demo.sh — Fault injection scripts for the three demo scenarios
# Usage: ./scripts/demo.sh <scenario>
#   scenarios: error-rate | pod-crash | high-cpu | reset
# =============================================================================
set -euo pipefail

APP_URL="http://localhost:30080"
NAMESPACE="demo"

scenario="${1:-help}"

case "$scenario" in

  # ── Scenario 1: High Error Rate ─────────────────────────────────────────────
  error-rate)
    echo "🔴 Injecting HIGH ERROR RATE fault..."
    curl -s -X POST "$APP_URL/fault/errors" | jq .
    echo ""
    echo "Now generating traffic to raise error count:"
    for i in $(seq 1 50); do
      curl -s "$APP_URL/api/data" > /dev/null
    done
    echo "✓ Error rate fault active. Agent should detect within 30s."
    echo "  Watch: kubectl logs -n $NAMESPACE -l app=buggy-app -f"
    ;;

  # ── Scenario 2: Pod Crash Loop ───────────────────────────────────────────────
  pod-crash)
    echo "🔴 Simulating POD CRASH LOOP..."
    # Patch liveness probe to a bad path so pods repeatedly fail
    kubectl patch deployment buggy-app -n "$NAMESPACE" \
      --type='json' \
      -p='[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/httpGet/path","value":"/this-does-not-exist"}]'
    echo "✓ Liveness probe patched to bad path — pods will start restarting."
    echo "  Watch: kubectl get pods -n $NAMESPACE -w"
    echo ""
    echo "  To restore: ./scripts/demo.sh reset-probe"
    ;;

  # ── Scenario 3: High CPU ─────────────────────────────────────────────────────
  high-cpu)
    echo "🔴 Injecting HIGH CPU SPIKE fault..."
    curl -s -X POST "$APP_URL/fault/cpu" | jq .
    echo ""
    echo "✓ CPU spike active (60s). Agent should detect and scale up."
    echo "  Watch: kubectl top pods -n $NAMESPACE"
    ;;

  # ── Reset: restore everything ────────────────────────────────────────────────
  reset)
    echo "🟢 Resetting all faults..."
    curl -s -X POST "$APP_URL/fault/reset" | jq .
    echo "✓ Application faults cleared."
    ;;

  reset-probe)
    echo "🟢 Restoring liveness probe..."
    kubectl patch deployment buggy-app -n "$NAMESPACE" \
      --type='json' \
      -p='[{"op":"replace","path":"/spec/template/spec/containers/0/livenessProbe/httpGet/path","value":"/healthz"}]'
    echo "✓ Liveness probe restored to /healthz"
    ;;

  # ── Scenario 4: High Latency ────────────────────────────────────────────────
  latency)
    echo "🔴 Injecting HIGH LATENCY fault (600ms added to every request)..."
    curl -s -X POST "$APP_URL/fault/latency" -H "Content-Type: application/json" \
      -d '{"ms": 600}' | jq .
    echo "✓ Latency fault active. Agent should detect P99 > 200ms SLO breach."
    ;;

  # ── Scenario 5: Cascade Failure ─────────────────────────────────────────────
  cascade)
    echo "🔴 Injecting CASCADE FAILURE (errors + latency combined)..."
    curl -s -X POST "$APP_URL/fault/cascade" | jq .
    echo ""
    echo "Generating traffic to raise error count:"
    for i in $(seq 1 50); do curl -s "$APP_URL/api/data" > /dev/null; done
    echo "✓ Cascade fault active — multiple SLO breaches simultaneously."
    ;;

  # ── Show current fault status ────────────────────────────────────────────────
  status)
    echo "📊 Current fault status:"
    curl -s "$APP_URL/fault/status" | jq .
    ;;

  # ── Traffic generator (background load) ─────────────────────────────────────
  load)
    echo "Generating continuous traffic (Ctrl+C to stop)..."
    while true; do
      curl -s "$APP_URL/api/data" > /dev/null
      sleep 0.5
    done
    ;;

  *)
    echo "Usage: $0 <scenario>"
    echo ""
    echo "  Scenarios:"
    echo "    error-rate   Inject high HTTP 500 error rate (>1% SLO breach)"
    echo "    pod-crash    Trigger pod crash loop via bad liveness probe"
    echo "    high-cpu     Inject CPU spike to trigger scale-up"
    echo "    latency      Inject 600ms latency — P99 SLO breach"
    echo "    cascade      Errors + latency together — multi-SLO breach"
    echo "    status       Show current fault state"
    echo "    reset        Clear all application faults"
    echo "    reset-probe  Restore liveness probe after pod-crash scenario"
    echo "    load         Generate continuous background traffic"
    ;;
esac
