#!/usr/bin/env bash
# =============================================================================
# load_gen.sh — Continuous background traffic generator for buggy-app
#
# Sends requests to /api/data at a steady rate so Prometheus always has
# fresh data. Without this, fault-injected errors never appear in metrics
# because rate(http_requests_total[2m]) requires actual traffic.
#
# Usage:
#   ./scripts/load_gen.sh                  # 2 req/s (default)
#   ./scripts/load_gen.sh --rps 5          # 5 req/s
#   ./scripts/load_gen.sh --rps 1          # 1 req/s (light)
#   ./scripts/load_gen.sh --silent         # no per-request output
# =============================================================================

APP_URL="http://localhost:30080"
RPS=2
SILENT=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rps)    shift; RPS="$1" ;;
    --rps=*)  RPS="${1#*=}" ;;
    --silent) SILENT=true ;;
    -h|--help)
      echo "Usage: $0 [--rps N] [--silent]"
      exit 0
      ;;
  esac
  shift
done

INTERVAL=$(echo "scale=4; 1 / $RPS" | bc)

echo "Load generator started — ${RPS} req/s to ${APP_URL}/api/data"
echo "Press Ctrl+C to stop."
echo ""

ok=0; err=0; total=0

while true; do
  http_code=$(curl -s -o /dev/null -w "%{http_code}" \
    --connect-timeout 2 --max-time 3 \
    "${APP_URL}/api/data" 2>/dev/null)

  total=$((total + 1))
  if [[ "$http_code" =~ ^(200|201)$ ]]; then
    ok=$((ok + 1))
  else
    err=$((err + 1))
  fi

  if ! $SILENT; then
    # Overwrite same line — shows running totals
    printf "\r  requests: %-6d  ok: %-6d  errors: %-6d  last: HTTP %s" \
      "$total" "$ok" "$err" "$http_code"
  fi

  sleep "$INTERVAL"
done
