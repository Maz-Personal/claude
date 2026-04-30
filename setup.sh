#!/bin/bash
# ══════════════════════════════════════════════════════════════════
#  VPS Setup Script — Trading Bots
#  Run once on a fresh Ubuntu 22.04 / 24.04 Droplet:
#    bash setup.sh
# ══════════════════════════════════════════════════════════════════

set -e

REPO="https://github.com/Maz-Personal/claude"
INSTALL_DIR="$HOME/trading"

echo ""
echo "══════════════════════════════════════════════════════"
echo "  Trading Bot VPS Setup"
echo "══════════════════════════════════════════════════════"
echo ""

# ── 1. System packages ────────────────────────────────────────────
echo "▶ Installing system packages..."
apt-get update -q
apt-get install -y -q python3 python3-pip python3-venv git

# ── 2. Clone repo ─────────────────────────────────────────────────
echo "▶ Cloning repo..."
if [ -d "$INSTALL_DIR" ]; then
    echo "  Directory exists — pulling latest..."
    git -C "$INSTALL_DIR" pull
else
    git clone "$REPO" "$INSTALL_DIR"
fi

# ── 3. Python virtual environment + dependencies ──────────────────
echo "▶ Setting up Python environment..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

PYTHON="$INSTALL_DIR/.venv/bin/python"

# ── 4. .env credentials ───────────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo ""
    echo "▶ Setting up credentials..."
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"

    echo ""
    echo "  Enter your Alpaca API credentials:"
    echo ""

    read -p "  WHEEL_ALPACA_API_KEY:          " WHEEL_KEY
    read -p "  WHEEL_ALPACA_API_SECRET:       " WHEEL_SECRET
    read -p "  TRAILING_ALPACA_API_KEY:       " TRAILING_KEY
    read -p "  TRAILING_ALPACA_API_SECRET:    " TRAILING_SECRET
    read -p "  CAPITOL_ALPACA_API_KEY:        " CAPITOL_KEY
    read -p "  CAPITOL_ALPACA_API_SECRET:     " CAPITOL_SECRET

    sed -i "s|WHEEL_ALPACA_API_KEY=.*|WHEEL_ALPACA_API_KEY=$WHEEL_KEY|" "$INSTALL_DIR/.env"
    sed -i "s|WHEEL_ALPACA_API_SECRET=.*|WHEEL_ALPACA_API_SECRET=$WHEEL_SECRET|" "$INSTALL_DIR/.env"
    sed -i "s|TRAILING_ALPACA_API_KEY=.*|TRAILING_ALPACA_API_KEY=$TRAILING_KEY|" "$INSTALL_DIR/.env"
    sed -i "s|TRAILING_ALPACA_API_SECRET=.*|TRAILING_ALPACA_API_SECRET=$TRAILING_SECRET|" "$INSTALL_DIR/.env"
    sed -i "s|CAPITOL_ALPACA_API_KEY=.*|CAPITOL_ALPACA_API_KEY=$CAPITOL_KEY|" "$INSTALL_DIR/.env"
    sed -i "s|CAPITOL_ALPACA_API_SECRET=.*|CAPITOL_ALPACA_API_SECRET=$CAPITOL_SECRET|" "$INSTALL_DIR/.env"

    chmod 600 "$INSTALL_DIR/.env"
    echo ""
    echo "  ✓ .env saved"
else
    echo "▶ .env already exists — skipping credential setup"
fi

# ── 5. Cron jobs ──────────────────────────────────────────────────
echo ""
echo "▶ Setting up cron jobs..."

CRON_CAPITOL="*/30 9-16 * * 1-5 $PYTHON $INSTALL_DIR/capitol_copier/bot.py >> $INSTALL_DIR/capitol_copier/bot.log 2>&1"
CRON_TRAILING="*/5 9-16 * * 1-5 $PYTHON \"$INSTALL_DIR/main account/trailing_stop.py\" --once >> $INSTALL_DIR/main\ account/trailing_stop.log 2>&1"

# Add crons (skip if already present)
( crontab -l 2>/dev/null | grep -v "capitol_copier/bot.py" | grep -v "trailing_stop.py"; \
  echo "$CRON_CAPITOL"; \
  echo "$CRON_TRAILING" ) | crontab -

echo "  ✓ Cron jobs registered"
echo ""
crontab -l

# ── 6. Test connections ───────────────────────────────────────────
echo ""
echo "▶ Testing Alpaca connections..."
$PYTHON - <<EOF
import os
from dotenv import load_dotenv
load_dotenv("$INSTALL_DIR/.env")
from alpaca.trading.client import TradingClient

tests = [
    ("Wheel",         os.getenv("WHEEL_ALPACA_API_KEY"),    os.getenv("WHEEL_ALPACA_API_SECRET")),
    ("Trailing Stop", os.getenv("TRAILING_ALPACA_API_KEY"), os.getenv("TRAILING_ALPACA_API_SECRET")),
    ("Capitol Copier",os.getenv("CAPITOL_ALPACA_API_KEY"),  os.getenv("CAPITOL_ALPACA_API_SECRET")),
]
for name, key, secret in tests:
    if not key or "your_" in key:
        print(f"  ⚠  {name}: not configured")
        continue
    try:
        acct = TradingClient(key, secret, paper=True).get_account()
        print(f"  ✓  {name}: connected — equity \${float(acct.equity):,.2f}")
    except Exception as e:
        print(f"  ✗  {name}: FAILED — {e}")
EOF

echo ""
echo "══════════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Bots will run automatically via cron."
echo "  Logs:"
echo "    Capitol Copier : $INSTALL_DIR/capitol_copier/bot.log"
echo "    Trailing Stop  : $INSTALL_DIR/main account/trailing_stop.log"
echo ""
echo "  To update bots from GitHub:"
echo "    git -C $INSTALL_DIR pull"
echo "══════════════════════════════════════════════════════"
echo ""
