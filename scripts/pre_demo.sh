#!/usr/bin/env bash
# Run ONCE before: vhs demo_short.tape
# Injects fault and waits for rate(30s) window to fill so the GIF starts at the breach.
set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
APP_URL="http://localhost:30080"

echo "► Resetting buggy-app to 1 replica..."
kubectl scale deployment/buggy-app --replicas=1 -n demo
kubectl rollout status deployment/buggy-app -n demo --timeout=60s

echo "► Starting Loki port-forward..."
pkill -f "kubectl port-forward svc/loki 3100" 2>/dev/null || true
sleep 1
kubectl port-forward svc/loki 3100:3100 -n monitoring &>/dev/null &
sleep 2

echo "► Injecting CPU fault..."
curl -s -X POST "${APP_URL}/fault/reset" >/dev/null
sleep 1
curl -s -X POST "${APP_URL}/fault/cpu"
echo ""

echo "► Waiting 35s for rate(30s) window to fill..."
sleep 35

echo ""
echo "✓ Ready. Run: vhs demo_short.tape"
