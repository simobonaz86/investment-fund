#!/usr/bin/env bash
# deploy.sh — run this directly on Sentinel to bootstrap or re-deploy
# Usage:  ANTHROPIC_API_KEY=sk-ant-... bash deploy.sh
set -euo pipefail

REPO="https://github.com/simobonaz86/investment-fund.git"
APP_DIR="/opt/investment_fund"
DATA_DIR="/opt/investment_fund_data"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

log()  { echo -e "${GREEN}==>${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
die()  { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ── Pre-flight ────────────────────────────────────────────────────────────────
[[ -z "${ANTHROPIC_API_KEY:-}" ]] && die "ANTHROPIC_API_KEY is not set"
command -v docker >/dev/null     || die "docker not found"
command -v git    >/dev/null     || die "git not found"

log "Starting Investment Fund deploy on $(hostname)"

# ── Clone / pull ──────────────────────────────────────────────────────────────
if [[ -d "$APP_DIR/.git" ]]; then
    log "Pulling latest code → $APP_DIR"
    git -C "$APP_DIR" pull origin main
else
    log "Cloning $REPO → $APP_DIR"
    git clone "$REPO" "$APP_DIR"
fi

mkdir -p "$DATA_DIR"

# ── Write .env ────────────────────────────────────────────────────────────────
log "Writing .env"
cat > "$APP_DIR/.env" <<EOF
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
MARKET_SIM_URL=http://market_sim:8001
DB_PATH=/data/fund.db
ASSETS=SYN-A,SYN-B,SYN-C
MOMENTUM_THRESHOLD=0.03
CONFIDENCE_THRESHOLD=0.70
MAX_POSITION_USD=1000.0
CHECK_INTERVAL_SECONDS=60
MANAGER_MODEL=anthropic/claude-haiku-4-5-20251001
SPECIALIST_MODEL=anthropic/claude-haiku-4-5-20251001
LOG_LEVEL=INFO
EOF

# ── Patch docker-compose to use persistent host volume ───────────────────────
# Ensure data is kept outside the container across redeploys
sed -i "s|fund_data:|fund_data:\n    driver: local\n    driver_opts:\n      type: none\n      o: bind\n      device: $DATA_DIR|" \
    "$APP_DIR/docker-compose.yml" 2>/dev/null || warn "volume patch skipped (already applied?)"

# ── Build + start ─────────────────────────────────────────────────────────────
log "Building images"
docker compose -f "$APP_DIR/docker-compose.yml" build

log "Starting services"
docker compose -f "$APP_DIR/docker-compose.yml" --env-file "$APP_DIR/.env" \
    up -d --remove-orphans

# ── Health check ─────────────────────────────────────────────────────────────
log "Waiting 10 s for market_sim to warm up …"
sleep 10

HEALTH=$(curl -sf http://localhost:8001/health 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK assets:', list(d['assets'].keys()))" 2>/dev/null || echo "NOT READY")
echo "  market_sim: $HEALTH"

docker compose -f "$APP_DIR/docker-compose.yml" ps

log "Deploy complete. Logs:"
echo "  docker compose -f $APP_DIR/docker-compose.yml logs -f fund"
