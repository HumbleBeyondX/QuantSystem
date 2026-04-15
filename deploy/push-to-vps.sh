#!/usr/bin/env bash
# ============================================================
# QuantSystem — Push to VPS & Deploy
# ============================================================
# Usage:
#   bash deploy/push-to-vps.sh user@your-vps-ip
#
# This will:
#   1. Package the essential code (no node_modules, .git, etc.)
#   2. Upload to VPS:/opt/quantsystem/
#   3. Build Docker image on VPS
#   4. Restart the container
#
# First-time setup: also run setup-vps.sh on the VPS
# ============================================================

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: bash deploy/push-to-vps.sh user@vps-ip"
    echo ""
    echo "Example:"
    echo "  bash deploy/push-to-vps.sh root@43.159.xxx.xxx"
    exit 1
fi

VPS="$1"
REMOTE_DIR="/opt/quantsystem"
ARCHIVE="qs-live-$(date +%Y%m%d%H%M%S).tar.gz"

echo "[1/4] Packaging code..."
tar czf "/tmp/$ARCHIVE" \
    --exclude='.git' \
    --exclude='node_modules' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.venv' \
    --exclude='desktop' \
    --exclude='dashboard/frontend' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='research' \
    --exclude='notebooks' \
    --exclude='*.egg-info' \
    -C "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" \
    core/ strategies/ execution/ portfolio/ backtesting/ \
    config/ dashboard/backend/ deploy/

echo "   Archive: /tmp/$ARCHIVE ($(du -h /tmp/$ARCHIVE | cut -f1))"

echo "[2/4] Uploading to $VPS..."
ssh "$VPS" "mkdir -p $REMOTE_DIR"
scp "/tmp/$ARCHIVE" "$VPS:/tmp/$ARCHIVE"

echo "[3/4] Extracting on VPS..."
ssh "$VPS" "cd $REMOTE_DIR && tar xzf /tmp/$ARCHIVE && rm /tmp/$ARCHIVE"

echo "[4/4] Rebuilding and restarting container..."
ssh "$VPS" "cd $REMOTE_DIR && docker compose -f deploy/docker-compose.yml up -d --build"

echo ""
echo "✅ Deployed! Check status:"
echo "   ssh $VPS 'curl -s http://127.0.0.1:8002/api/live/runner/status | python3 -m json.tool'"
echo "   ssh $VPS 'docker compose -f $REMOTE_DIR/deploy/docker-compose.yml logs -f'"

rm -f "/tmp/$ARCHIVE"
