#!/usr/bin/env bash
# ============================================================
# QuantSystem VPS One-Command Setup
# ============================================================
# 在全新的 Ubuntu 22.04/24.04 VPS 上运行:
#
#   curl -sSL https://raw.githubusercontent.com/YOUR_REPO/deploy/setup-vps.sh | bash
#   — 或 —
#   bash deploy/setup-vps.sh
#
# 前提:
#   1. 已经有一台海外VPS (推荐: AWS Tokyo/Singapore, DigitalOcean SGP, Vultr Tokyo)
#   2. VPS 允许 crypto 交易 (避免美国/中国大陆节点)
#   3. 已经SSH连接到VPS
#
# 此脚本会:
#   - 安装 Docker + Docker Compose
#   - 克隆/上传代码
#   - 配置 .env (你需要手动填入API key)
#   - 启动 live trading container
#   - 配置 systemd 自动重启
#   - 配置日志轮转
#   - 配置防火墙 (只开放SSH)
# ============================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[QS]${NC} $1"; }
warn() { echo -e "${YELLOW}[QS]${NC} $1"; }
err()  { echo -e "${RED}[QS]${NC} $1"; exit 1; }

# ── Check we're on Linux ──
[[ "$(uname)" == "Linux" ]] || err "This script is for Linux VPS only"

# ── Check root/sudo ──
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
else
    SUDO=""
fi

APP_DIR="/opt/quantsystem"
COMPOSE_FILE="$APP_DIR/deploy/docker-compose.yml"

# ============================================================
# Step 1: System Update & Docker Install
# ============================================================
log "Step 1/6: Installing Docker..."

if command -v docker &>/dev/null; then
    log "  Docker already installed: $(docker --version)"
else
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq apt-transport-https ca-certificates curl gnupg lsb-release

    # Docker official GPG key
    $SUDO install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
    $SUDO chmod a+r /etc/apt/keyrings/docker.gpg

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null

    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin

    # Let non-root user run docker
    if [[ $EUID -ne 0 ]]; then
        $SUDO usermod -aG docker "$USER"
        warn "  Added $USER to docker group. You may need to re-login."
    fi

    log "  Docker installed: $(docker --version)"
fi

# ============================================================
# Step 2: Create application directory
# ============================================================
log "Step 2/6: Setting up application directory..."

if [[ -d "$APP_DIR" ]]; then
    log "  $APP_DIR already exists"
else
    $SUDO mkdir -p "$APP_DIR"
    $SUDO chown "$(whoami):$(whoami)" "$APP_DIR"
    log "  Created $APP_DIR"
fi

# If running from repo, copy files
if [[ -f "deploy/Dockerfile" ]]; then
    log "  Copying project files to $APP_DIR..."
    rsync -a --exclude='.git' --exclude='node_modules' --exclude='__pycache__' \
          --exclude='.venv' --exclude='desktop' --exclude='dashboard/frontend' \
          --exclude='*.pyc' --exclude='.env' \
          ./ "$APP_DIR/"
    log "  Files copied"
else
    warn "  No local repo found. Please upload your code to $APP_DIR"
    warn "  Example: scp -r /path/to/QuantSystem/* user@vps:$APP_DIR/"
fi

# ============================================================
# Step 3: Configure environment variables
# ============================================================
log "Step 3/6: Configuring environment..."

ENV_FILE="$APP_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    log "  .env already exists"
else
    cat > "$ENV_FILE" << 'ENVEOF'
# ============================================================
# QuantSystem Live Trading — Environment Variables
# ============================================================
# IMPORTANT: Fill in your OKX API credentials below
# Generate at: https://www.okx.com/account/my-api

# OKX API Credentials
OKX_API_KEY=your_api_key_here
OKX_API_SECRET=your_api_secret_here
OKX_PASSPHRASE=your_passphrase_here

# Strategy config to auto-start
QS_LIVE_CONFIG=config/live/eth_sniper_live.yaml

# Optional: Telegram notifications
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=
ENVEOF
    chmod 600 "$ENV_FILE"
    warn "  Created $ENV_FILE — EDIT THIS FILE with your OKX API keys!"
    warn "  Run: nano $ENV_FILE"
fi

# ============================================================
# Step 4: Build and start Docker container
# ============================================================
log "Step 4/6: Building Docker image..."

cd "$APP_DIR"
$SUDO docker compose -f deploy/docker-compose.yml build

# Check if .env has real keys
if grep -q "your_api_key_here" "$ENV_FILE"; then
    warn ""
    warn "  ⚠️  .env still has placeholder keys!"
    warn "  Edit: nano $ENV_FILE"
    warn "  Then start: docker compose -f deploy/docker-compose.yml up -d"
    warn ""
else
    log "  Starting live trading container..."
    $SUDO docker compose -f deploy/docker-compose.yml up -d
    log "  Container started!"
fi

# ============================================================
# Step 5: Firewall (only SSH + local dashboard)
# ============================================================
log "Step 5/6: Configuring firewall..."

if command -v ufw &>/dev/null; then
    $SUDO ufw --force enable 2>/dev/null || true
    $SUDO ufw allow ssh 2>/dev/null || true
    # Dashboard only on localhost (port 8002 is bound to 127.0.0.1)
    log "  UFW enabled: SSH allowed, dashboard on localhost:8002 only"
else
    log "  UFW not found, skipping firewall setup"
fi

# ============================================================
# Step 6: Systemd watchdog (auto-restart on reboot)
# ============================================================
log "Step 6/6: Setting up auto-start on boot..."

SYSTEMD_FILE="/etc/systemd/system/quantsystem.service"
$SUDO tee "$SYSTEMD_FILE" > /dev/null << EOF
[Unit]
Description=QuantSystem Live Trading
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/docker compose -f deploy/docker-compose.yml up -d
ExecStop=/usr/bin/docker compose -f deploy/docker-compose.yml down
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable quantsystem.service
log "  Systemd service enabled (auto-start on reboot)"

# ============================================================
# Done!
# ============================================================
echo ""
echo -e "${CYAN}============================================================${NC}"
echo -e "${CYAN}  QuantSystem Live Trading — VPS Setup Complete!${NC}"
echo -e "${CYAN}============================================================${NC}"
echo ""
echo -e "  ${GREEN}Application:${NC}  $APP_DIR"
echo -e "  ${GREEN}Config:${NC}       $ENV_FILE"
echo -e "  ${GREEN}Dashboard:${NC}    http://127.0.0.1:8002 (localhost only)"
echo ""
echo -e "  ${YELLOW}Commands:${NC}"
echo -e "    View logs:     docker compose -f $COMPOSE_FILE logs -f"
echo -e "    Stop:          docker compose -f $COMPOSE_FILE down"
echo -e "    Restart:       docker compose -f $COMPOSE_FILE restart"
echo -e "    Status:        docker compose -f $COMPOSE_FILE ps"
echo -e "    Check runner:  curl -s http://127.0.0.1:8002/api/live/runner/status | python3 -m json.tool"
echo ""
if grep -q "your_api_key_here" "$ENV_FILE" 2>/dev/null; then
    echo -e "  ${RED}⚠️  NEXT STEP: Edit .env with your OKX API keys:${NC}"
    echo -e "    nano $ENV_FILE"
    echo -e "    docker compose -f $COMPOSE_FILE up -d"
fi
echo ""
