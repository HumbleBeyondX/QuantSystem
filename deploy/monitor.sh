#!/usr/bin/env bash
# ============================================================
# QuantSystem Live Monitoring — Quick Status Check
# ============================================================
# Usage: bash deploy/monitor.sh
# ============================================================

set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

API="http://127.0.0.1:8002"

echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║     QuantSystem Live Trading Monitor         ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ── Health Check ──
health=$(curl -sf "$API/health" 2>/dev/null || echo '{"status":"offline"}')
status=$(echo "$health" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','offline'))" 2>/dev/null || echo "offline")

if [[ "$status" == "healthy" ]]; then
    echo -e "  Backend:     ${GREEN}● ONLINE${NC}"
else
    echo -e "  Backend:     ${RED}● OFFLINE${NC}"
    echo ""
    echo -e "  ${RED}Backend is not running. Start with:${NC}"
    echo -e "  docker compose -f deploy/docker-compose.yml up -d"
    exit 1
fi

# ── Runner Status ──
runner=$(curl -sf "$API/api/live/runner/status" 2>/dev/null || echo '{}')

running=$(echo "$runner" | python3 -c "import sys,json; print(json.load(sys.stdin).get('running', False))" 2>/dev/null || echo "False")
strategy=$(echo "$runner" | python3 -c "import sys,json; print(json.load(sys.stdin).get('strategy_id', 'N/A'))" 2>/dev/null || echo "N/A")
exchange=$(echo "$runner" | python3 -c "import sys,json; print(json.load(sys.stdin).get('exchange', 'N/A'))" 2>/dev/null || echo "N/A")
symbol=$(echo "$runner" | python3 -c "import sys,json; print(json.load(sys.stdin).get('symbol', 'N/A'))" 2>/dev/null || echo "N/A")
pnl=$(echo "$runner" | python3 -c "import sys,json; print(f\"\${json.load(sys.stdin).get('session_pnl', 0):.4f}\")" 2>/dev/null || echo "$0.00")
trades=$(echo "$runner" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_trades', 0))" 2>/dev/null || echo "0")
orders=$(echo "$runner" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_orders', 0))" 2>/dev/null || echo "0")
started=$(echo "$runner" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_start', 'N/A')[:19])" 2>/dev/null || echo "N/A")
emergency=$(echo "$runner" | python3 -c "import sys,json; print(json.load(sys.stdin).get('emergency_stopped', False))" 2>/dev/null || echo "False")

if [[ "$running" == "True" ]]; then
    if [[ "$emergency" == "True" ]]; then
        echo -e "  Runner:      ${RED}● EMERGENCY STOPPED${NC}"
    else
        echo -e "  Runner:      ${GREEN}● RUNNING${NC}"
    fi
else
    echo -e "  Runner:      ${YELLOW}● INACTIVE${NC}"
fi

echo -e "  Strategy:    ${strategy}"
echo -e "  Exchange:    ${exchange} / ${symbol}"
echo -e "  Started:     ${started}"
echo ""
echo -e "  ${CYAN}── Session Stats ──${NC}"
echo -e "  PnL:         ${pnl}"
echo -e "  Trades:      ${trades}"
echo -e "  Orders:      ${orders}"
echo ""

# ── Docker Container Status ──
echo -e "  ${CYAN}── Container ──${NC}"
if command -v docker &>/dev/null; then
    container=$(docker ps --filter "name=qs-live-trader" --format "{{.Status}}" 2>/dev/null || echo "N/A")
    if [[ -n "$container" ]]; then
        echo -e "  Docker:      ${GREEN}${container}${NC}"
    else
        echo -e "  Docker:      ${YELLOW}Not running as container${NC}"
    fi

    # Show last 5 log lines
    echo ""
    echo -e "  ${CYAN}── Recent Logs ──${NC}"
    docker logs qs-live-trader --tail 5 2>/dev/null || echo "  (no container logs)"
fi
echo ""
