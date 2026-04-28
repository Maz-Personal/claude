import os
import time
import json
from datetime import datetime
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

load_dotenv()

API_KEY    = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_API_SECRET")

trading = TradingClient(API_KEY, API_SECRET, paper=True)
data    = StockHistoricalDataClient(API_KEY, API_SECRET)

SYMBOL        = "TSLA"
INITIAL_QTY   = 10
POLL_SECS     = 30

# ── helpers ──────────────────────────────────────────────────────────────────

def now():
    return datetime.now().strftime("%H:%M:%S")

def get_price(symbol):
    resp = data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
    return float(resp[symbol].price)

def place(symbol, qty, side):
    return trading.submit_order(MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    ))

def log_order(label, order, extra=""):
    print(f"  [{now()}] {label}")
    print(f"           ID     : {order.id}")
    print(f"           Symbol : {order.symbol}  |  Qty: {order.qty}  |  Side: {order.side.value.upper()}")
    print(f"           Status : {order.status.value}")
    if extra:
        print(f"           {extra}")
    print()

# ── 1. Initial buy ────────────────────────────────────────────────────────────

print("=" * 60)
print("  TSLA TRAILING STOP STRATEGY  —  paper trading")
print("=" * 60)
print()

print(">>> Placing initial market buy: 10 x TSLA")
initial_order = place(SYMBOL, INITIAL_QTY, OrderSide.BUY)
log_order("INITIAL BUY", initial_order)

time.sleep(2)  # let the order register
entry_price = get_price(SYMBOL)

# ── Strategy parameters ───────────────────────────────────────────────────────

stop_loss        = round(entry_price * 0.90, 2)   # 10% floor
trail_trigger    = round(entry_price * 1.10, 2)   # price that activates trailing

# Ladders — buy MORE at deeper discounts (better value = bigger bet)
ladder_15_price  = round(entry_price * 0.85, 2)   # -15% → buy 10 shares
ladder_25_price  = round(entry_price * 0.75, 2)   # -25% → buy 20 shares
ladder_35_price  = round(entry_price * 0.65, 2)   # -35% → buy 30 shares
ladder_50_price  = round(entry_price * 0.50, 2)   # -50% → buy 40 shares

print("=" * 60)
print("  STRATEGY SUMMARY")
print("=" * 60)
print(f"  Entry price         : ${entry_price:.2f}")
print(f"  Initial stop loss   : ${stop_loss:.2f}  (entry - 10%)")
print()
print(f"  Trailing stop       : activates when price reaches ${trail_trigger:.2f} (+10%)")
print(f"                        then rides 5% below running high -- floor only goes UP")
print()
print(f"  Ladder -15%         : buy 10 shares  if price hits ${ladder_15_price:.2f}")
print(f"  Ladder -25%         : buy 20 shares  if price hits ${ladder_25_price:.2f}")
print(f"  Ladder -35%         : buy 30 shares  if price hits ${ladder_35_price:.2f}")
print(f"  Ladder -50%         : buy 40 shares  if price hits ${ladder_50_price:.2f}")
print()
print(f"  Polling interval    : every {POLL_SECS}s")
print("=" * 60)
print()

# save state so we can resume if the script is restarted
state = {
    "symbol": SYMBOL,
    "entry_price": entry_price,
    "stop_loss": stop_loss,
    "total_qty": INITIAL_QTY,
    "trail_trigger": trail_trigger,
    "trailing_active": False,
    "ladder_15_triggered": False,
    "ladder_25_triggered": False,
    "ladder_35_triggered": False,
    "ladder_50_triggered": False,
    "started_at": datetime.now().isoformat(),
}
with open("tsla_strategy_state.json", "w") as f:
    json.dump(state, f, indent=2)

# ── 2. Monitor loop ───────────────────────────────────────────────────────────

print(">>> Monitoring started. Press Ctrl+C to stop.\n")

total_qty        = state["total_qty"]
trailing_active  = False
l15_triggered    = False
l25_triggered    = False
l35_triggered    = False
l50_triggered    = False
position_closed  = False

while not position_closed:
    try:
        price   = get_price(SYMBOL)
        pct     = (price - entry_price) / entry_price * 100

        # ── trailing stop adjustment ──────────────────────────────────────────
        if price >= trail_trigger:
            candidate = round(price * 0.95, 2)
            if candidate > stop_loss:
                old_stop  = stop_loss
                stop_loss = candidate
                trailing_active = True
                print(f"  [{now()}] TRAILING  TSLA ${price:.2f} ({pct:+.2f}%)  "
                      f"stop raised ${old_stop:.2f} -> ${stop_loss:.2f}")
                state.update({"stop_loss": stop_loss, "trailing_active": True})
                with open("tsla_strategy_state.json", "w") as f:
                    json.dump(state, f, indent=2)

        # ── stop loss hit → sell all ──────────────────────────────────────────
        if price <= stop_loss:
            print(f"\n  [{now()}] *** STOP LOSS TRIGGERED ***  "
                  f"TSLA ${price:.2f}  stop was ${stop_loss:.2f}")
            print(f"           Selling all {total_qty} shares ...")
            sell_order = place(SYMBOL, total_qty, OrderSide.SELL)
            log_order("STOP LOSS SELL", sell_order,
                      f"Triggered at ${price:.2f}  ({pct:+.2f}% from entry)")
            position_closed = True

        # ── ladder -50% (check deepest first to avoid double-fire) ───────────
        elif pct <= -50 and not l50_triggered:
            l50_triggered = True
            print(f"\n  [{now()}] *** LADDER -50% ***  "
                  f"TSLA ${price:.2f}  buying 40 shares ...")
            ladder_order = place(SYMBOL, 40, OrderSide.BUY)
            total_qty += 40
            state.update({"total_qty": total_qty, "ladder_50_triggered": True})
            with open("tsla_strategy_state.json", "w") as f:
                json.dump(state, f, indent=2)
            log_order("LADDER BUY -50%", ladder_order,
                      f"Total position now: {total_qty} shares")

        # ── ladder -35% ───────────────────────────────────────────────────────
        elif pct <= -35 and not l35_triggered:
            l35_triggered = True
            print(f"\n  [{now()}] *** LADDER -35% ***  "
                  f"TSLA ${price:.2f}  buying 30 shares ...")
            ladder_order = place(SYMBOL, 30, OrderSide.BUY)
            total_qty += 30
            state.update({"total_qty": total_qty, "ladder_35_triggered": True})
            with open("tsla_strategy_state.json", "w") as f:
                json.dump(state, f, indent=2)
            log_order("LADDER BUY -35%", ladder_order,
                      f"Total position now: {total_qty} shares")

        # ── ladder -25% ───────────────────────────────────────────────────────
        elif pct <= -25 and not l25_triggered:
            l25_triggered = True
            print(f"\n  [{now()}] *** LADDER -25% ***  "
                  f"TSLA ${price:.2f}  buying 20 shares ...")
            ladder_order = place(SYMBOL, 20, OrderSide.BUY)
            total_qty += 20
            state.update({"total_qty": total_qty, "ladder_25_triggered": True})
            with open("tsla_strategy_state.json", "w") as f:
                json.dump(state, f, indent=2)
            log_order("LADDER BUY -25%", ladder_order,
                      f"Total position now: {total_qty} shares")

        # ── ladder -15% ───────────────────────────────────────────────────────
        elif pct <= -15 and not l15_triggered:
            l15_triggered = True
            print(f"\n  [{now()}] *** LADDER -15% ***  "
                  f"TSLA ${price:.2f}  buying 10 shares ...")
            ladder_order = place(SYMBOL, 10, OrderSide.BUY)
            total_qty += 10
            state.update({"total_qty": total_qty, "ladder_15_triggered": True})
            with open("tsla_strategy_state.json", "w") as f:
                json.dump(state, f, indent=2)
            log_order("LADDER BUY -15%", ladder_order,
                      f"Total position now: {total_qty} shares")

        else:
            trail_flag = "TRAILING" if trailing_active else "FIXED"
            print(f"  [{now()}]  TSLA ${price:.2f} ({pct:+.2f}%)  "
                  f"stop ${stop_loss:.2f} [{trail_flag}]  position {total_qty} shares")

        if not position_closed:
            time.sleep(POLL_SECS)

    except KeyboardInterrupt:
        print("\n\nMonitor stopped by user. Strategy state saved to tsla_strategy_state.json")
        break

if position_closed:
    print("Strategy complete — position fully closed.")
