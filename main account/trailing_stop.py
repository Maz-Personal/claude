"""
Trailing Stop + Ladder Buy Strategy — Paper Trading (Multi-Ticker)
═══════════════════════════════════════════════════════════════════
Usage:
    python trailing_stop.py TSLA                  ← single ticker, defaults
    python trailing_stop.py TSLA NVDA AAPL        ← multiple tickers
    python trailing_stop.py TSLA --qty 20         ← 20 shares initial buy
    python trailing_stop.py TSLA --stop 0.08      ← 8% initial stop instead of 10%
    python trailing_stop.py TSLA --trail-pct 0.05 ← 5% trailing (default)
    python trailing_stop.py TSLA --no-ladder      ← disable ladder buys

Strategy:
    1. Market buy initial shares
    2. Set stop loss at entry - 10%
    3. When price rises +10%, activate trailing stop (5% below running high)
    4. Ladder buy more shares at deeper discounts (-15%, -25%, -35%, -50%)
    5. If stop is hit, sell entire position
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

load_dotenv()

# Uses TRAILING_ALPACA_API_KEY / TRAILING_ALPACA_API_SECRET from .env
# Falls back to ALPACA_API_KEY / ALPACA_API_SECRET if not set
API_KEY    = os.getenv("TRAILING_ALPACA_API_KEY", os.getenv("ALPACA_API_KEY"))
API_SECRET = os.getenv("TRAILING_ALPACA_API_SECRET", os.getenv("ALPACA_API_SECRET"))

trading = TradingClient(API_KEY, API_SECRET, paper=True)
data    = StockHistoricalDataClient(API_KEY, API_SECRET)

# ── Default strategy parameters ───────────────────────────────────────────────
DEFAULTS = {
    "qty":              10,       # initial shares per ticker
    "stop_pct":         0.10,     # 10% initial stop loss
    "trail_trigger":    0.10,     # +10% activates trailing
    "trail_pct":        0.05,     # trail 5% below running high
    "poll_secs":        30,       # polling interval
    "ladder_enabled":   True,     # ladder buys on/off
    "ladders": [                  # (pct_drop, shares_to_buy)
        (-0.15, 10),
        (-0.25, 20),
        (-0.35, 30),
        (-0.50, 40),
    ],
}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(symbol, msg):
    print(f"  [{ts()}] [{symbol}] {msg}")

def get_price(symbol):
    resp = data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
    return float(resp[symbol].price)

def place_order(symbol, qty, side):
    return trading.submit_order(MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    ))

def log_order(symbol, label, order, extra=""):
    log(symbol, f"{label}")
    print(f"           ID     : {order.id}")
    print(f"           Symbol : {order.symbol}  |  Qty: {order.qty}  |  Side: {order.side.value.upper()}")
    print(f"           Status : {order.status.value}")
    if extra:
        print(f"           {extra}")
    print()


# ══════════════════════════════════════════════════════════════════════════════
#  STATE MANAGEMENT — one state file per ticker
# ══════════════════════════════════════════════════════════════════════════════

def state_file(symbol):
    return f"{symbol.lower()}_trailing_state.json"

def load_state(symbol):
    path = state_file(symbol)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None  # None = needs initial buy

def save_state(state):
    with open(state_file(state["symbol"]), "w") as f:
        json.dump(state, f, indent=2)

def init_state(symbol, entry_price, qty, cfg):
    """Create fresh state after initial buy."""
    state = {
        "symbol":           symbol,
        "entry_price":      entry_price,
        "stop_loss":        round(entry_price * (1 - cfg["stop_pct"]), 2),
        "total_qty":        qty,
        "trail_trigger":    round(entry_price * (1 + cfg["trail_trigger"]), 2),
        "trailing_active":  False,
        "position_closed":  False,
        "ladders_triggered": [],   # list of pct levels already triggered
        "started_at":       datetime.now().isoformat(),
    }
    save_state(state)
    return state


# ══════════════════════════════════════════════════════════════════════════════
#  PROCESS ONE TICKER (one iteration)
# ══════════════════════════════════════════════════════════════════════════════

def process_ticker(state, cfg):
    """Run one monitoring cycle for a single ticker. Returns updated state."""
    symbol = state["symbol"]

    if state.get("position_closed"):
        return state

    price       = get_price(symbol)
    entry_price = state["entry_price"]
    stop_loss   = state["stop_loss"]
    total_qty   = state["total_qty"]
    pct         = (price - entry_price) / entry_price * 100

    # ── Trailing stop adjustment ──────────────────────────────────────────────
    if price >= state["trail_trigger"]:
        candidate = round(price * (1 - cfg["trail_pct"]), 2)
        if candidate > stop_loss:
            old_stop = stop_loss
            stop_loss = candidate
            state["stop_loss"] = stop_loss
            state["trailing_active"] = True
            save_state(state)
            log(symbol, f"TRAILING  ${price:.2f} ({pct:+.2f}%)  "
                        f"stop raised ${old_stop:.2f} → ${stop_loss:.2f}")

    # ── Stop loss hit → sell all ──────────────────────────────────────────────
    if price <= stop_loss:
        log(symbol, f"*** STOP LOSS TRIGGERED ***  ${price:.2f}  stop was ${stop_loss:.2f}")
        log(symbol, f"Selling all {total_qty} shares ...")
        sell_order = place_order(symbol, total_qty, OrderSide.SELL)
        log_order(symbol, "STOP LOSS SELL", sell_order,
                  f"Triggered at ${price:.2f}  ({pct:+.2f}% from entry)")
        state["position_closed"] = True
        save_state(state)
        return state

    # ── Ladder buys ───────────────────────────────────────────────────────────
    if cfg["ladder_enabled"]:
        triggered = state.get("ladders_triggered", [])
        # Check deepest levels first to avoid double-fire
        for ladder_pct, ladder_qty in sorted(cfg["ladders"], key=lambda x: x[0]):
            if pct <= ladder_pct * 100 and ladder_pct not in triggered:
                triggered.append(ladder_pct)
                state["ladders_triggered"] = triggered
                log(symbol, f"*** LADDER {ladder_pct:.0%} ***  ${price:.2f}  "
                            f"buying {ladder_qty} shares ...")
                ladder_order = place_order(symbol, ladder_qty, OrderSide.BUY)
                total_qty += ladder_qty
                state["total_qty"] = total_qty
                save_state(state)
                log_order(symbol, f"LADDER BUY {ladder_pct:.0%}", ladder_order,
                          f"Total position now: {total_qty} shares")

    # ── Status line ───────────────────────────────────────────────────────────
    trail_flag = "TRAILING" if state.get("trailing_active") else "FIXED"
    log(symbol, f"${price:.2f} ({pct:+.2f}%)  "
                f"stop ${stop_loss:.2f} [{trail_flag}]  "
                f"position {total_qty} shares")

    return state


# ══════════════════════════════════════════════════════════════════════════════
#  CLI & MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Trailing Stop + Ladder Buy Strategy — any number of tickers",
        usage="python trailing_stop.py TICKER [TICKER ...] [options]",
    )
    parser.add_argument(
        "tickers", nargs="+", type=str,
        help="One or more stock ticker symbols (e.g. TSLA NVDA AAPL)",
    )
    parser.add_argument(
        "--qty", type=int, default=DEFAULTS["qty"],
        help=f"Initial shares per ticker (default: {DEFAULTS['qty']})",
    )
    parser.add_argument(
        "--stop", type=float, default=DEFAULTS["stop_pct"],
        help=f"Initial stop loss %% as decimal (default: {DEFAULTS['stop_pct']})",
    )
    parser.add_argument(
        "--trail-trigger", type=float, default=DEFAULTS["trail_trigger"],
        help=f"Price rise %% to activate trailing (default: {DEFAULTS['trail_trigger']})",
    )
    parser.add_argument(
        "--trail-pct", type=float, default=DEFAULTS["trail_pct"],
        help=f"Trailing stop distance %% (default: {DEFAULTS['trail_pct']})",
    )
    parser.add_argument(
        "--poll", type=int, default=DEFAULTS["poll_secs"],
        help=f"Polling interval in seconds (default: {DEFAULTS['poll_secs']})",
    )
    parser.add_argument(
        "--no-ladder", action="store_true",
        help="Disable ladder buys",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    tickers = [t.upper() for t in args.tickers]
    cfg = {
        "qty":            args.qty,
        "stop_pct":       args.stop,
        "trail_trigger":  args.trail_trigger,
        "trail_pct":      args.trail_pct,
        "poll_secs":      args.poll,
        "ladder_enabled": not args.no_ladder,
        "ladders":        DEFAULTS["ladders"],
    }

    print("=" * 64)
    print(f"  TRAILING STOP + LADDER BUY — Paper Trading")
    print(f"  Tickers        : {', '.join(tickers)}")
    print(f"  Initial qty    : {cfg['qty']} shares each")
    print(f"  Stop loss      : {cfg['stop_pct']:.0%}  |  Trail trigger: +{cfg['trail_trigger']:.0%}  |  "
          f"Trail distance: {cfg['trail_pct']:.0%}")
    print(f"  Ladders        : {'ON' if cfg['ladder_enabled'] else 'OFF'}")
    if cfg["ladder_enabled"]:
        ladder_str = "  ".join(f"{p:.0%}→+{q}" for p, q in cfg["ladders"])
        print(f"                   {ladder_str}")
    print(f"  Poll           : {cfg['poll_secs']}s")
    print(f"  Started        : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 64)
    print()

    # ── Load or initialize state per ticker ───────────────────────────────────
    states = []
    for symbol in tickers:
        existing = load_state(symbol)
        if existing and not existing.get("position_closed"):
            log(symbol, f"Resuming — entry ${existing['entry_price']:.2f}  "
                        f"stop ${existing['stop_loss']:.2f}  "
                        f"qty {existing['total_qty']}")
            states.append(existing)
        else:
            # Fresh start: place initial market buy
            log(symbol, f"Placing initial market buy: {cfg['qty']} shares")
            order = place_order(symbol, cfg["qty"], OrderSide.BUY)
            log_order(symbol, "INITIAL BUY", order)
            time.sleep(2)  # let the order register

            entry_price = get_price(symbol)
            state = init_state(symbol, entry_price, cfg["qty"], cfg)
            states.append(state)

            log(symbol, f"Entry: ${entry_price:.2f}  "
                        f"Stop: ${state['stop_loss']:.2f}  "
                        f"Trail activates at: ${state['trail_trigger']:.2f}")
            if cfg["ladder_enabled"]:
                for pct, qty in cfg["ladders"]:
                    lvl = round(entry_price * (1 + pct), 2)
                    log(symbol, f"  Ladder {pct:.0%}: buy {qty} shares @ ${lvl:.2f}")
            print()

    # ── Monitor loop ──────────────────────────────────────────────────────────
    print(">>> Monitoring started. Press Ctrl+C to stop.\n")

    while True:
        try:
            all_closed = True
            for i, state in enumerate(states):
                if not state.get("position_closed"):
                    all_closed = False
                    try:
                        states[i] = process_ticker(state, cfg)
                    except Exception as e:
                        log(state["symbol"], f"ERROR: {e}")

            if all_closed:
                print("\nAll positions closed. Strategy complete.")
                break

            time.sleep(cfg["poll_secs"])

        except KeyboardInterrupt:
            print("\n\nMonitor stopped. All states saved.")
            break


if __name__ == "__main__":
    main()
