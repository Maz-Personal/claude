"""
TradingAgent V18.9 — Complete Implementation
═════════════════════════════════════════════
State Machine: Pending → Open → Liquidated

Reads tickers from tickers.json by default (no args needed).
Each ticker runs in its own thread concurrently.

Usage:
    python trading_agent.py                ← reads all tickers from tickers.json
    python trading_agent.py NVDA           ← override: monitor NVDA only
    python trading_agent.py NVDA 20        ← override: NVDA, 20 shares

Credentials: AGENT_ALPACA_API_KEY / AGENT_ALPACA_API_SECRET in .env
             Falls back to WHEEL_ALPACA_API_KEY / WHEEL_ALPACA_API_SECRET
"""

import json
import time
import logging
import logging.handlers
import os
import sys
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, StopLimitOrderRequest, GetOrdersRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame

# ── Paths & credentials ───────────────────────────────────────────────────────
_DIR = Path(__file__).parent
load_dotenv(_DIR.parent / ".env")

API_KEY    = os.getenv("AGENT_ALPACA_API_KEY",    os.getenv("WHEEL_ALPACA_API_KEY"))
API_SECRET = os.getenv("AGENT_ALPACA_API_SECRET", os.getenv("WHEEL_ALPACA_API_SECRET"))

trading = TradingClient(API_KEY, API_SECRET, paper=True)
data    = StockHistoricalDataClient(API_KEY, API_SECRET)

# ── Logging ───────────────────────────────────────────────────────────────────
_log_file = _DIR / "trading_agent.log"
_fh = logging.handlers.RotatingFileHandler(
    _log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_fh, _ch])
log = logging.getLogger(__name__)

# ── Strategy config ───────────────────────────────────────────────────────────
STOP_PCT       = 0.10   # Initial stop loss: entry - 10%
BREAKEVEN_PCT  = 0.50   # Adjust stop to breakeven when PnL >= 50%
PROFIT_GATE    = 0.85   # Exit when PnL >= 85%
POLL_SECS      = 60     # Check every 60 seconds
EMA_FAST       = 20
EMA_SLOW       = 50
RSI_PERIOD     = 14
RSI_OVERBOUGHT = 70


# ══════════════════════════════════════════════════════════════════════════════
#  SHADOW LEDGER — V18.9 State Persistence
# ══════════════════════════════════════════════════════════════════════════════

class ShadowLedger:
    def __init__(self, ticker):
        self.filename = _DIR / f"{ticker.lower()}_shadow_ledger.json"
        self.state = self._load()

    def _load(self):
        if self.filename.exists():
            with open(self.filename) as f:
                return json.load(f)
        return {"positions": {}, "entry_price": None, "stop_price": None,
                "last_sync": None, "state": "PENDING"}

    def save(self, ticker, qty, reason, entry_price=None, stop_price=None, state=None):
        self.state["positions"][ticker] = qty
        self.state["last_sync"] = datetime.now(timezone.utc).isoformat()
        if entry_price is not None:
            self.state["entry_price"] = entry_price
        if stop_price is not None:
            self.state["stop_price"] = stop_price
        if state is not None:
            self.state["state"] = state
        with open(self.filename, "w") as f:
            json.dump(self.state, f, indent=2)
        log.info(f"Ledger Updated: {ticker} | Qty: {qty} | Reason: {reason} | State: {self.state['state']}")

    def get(self, key, default=None):
        return self.state.get(key, default)


# ══════════════════════════════════════════════════════════════════════════════
#  TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def ema(closes, period):
    k = 2 / (period + 1)
    e = closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    return round(e, 2)

def rsi(closes, period=14):
    gains = losses = 0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0: gains += d
        else:     losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + (d if d > 0 else 0)) / period
        avg_loss = (avg_loss * (period - 1) + (-d if d < 0 else 0)) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)

def get_bars(ticker, count=60):
    from datetime import timedelta
    start = datetime.now(timezone.utc) - timedelta(days=count * 2)  # extra buffer for weekends
    resp = data.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start,
        limit=count
    ))
    return resp[ticker]

def get_price(ticker):
    resp = data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
    return float(resp[ticker].price)


# ══════════════════════════════════════════════════════════════════════════════
#  STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════════

class TradeState(ABC):
    @abstractmethod
    def execute(self, context): pass

class StatePending(TradeState):
    def execute(self, context):
        log.info(f"[{context.ticker}] STATE: PENDING — checking entry signal...")
        if context.check_signals():
            log.info(f"[{context.ticker}] Signal verified. Entering position.", extra={})
            order = context.place_order(context.qty, OrderSide.BUY)
            if order:
                time.sleep(2)
                entry = get_price(context.ticker)
                stop  = round(entry * (1 - STOP_PCT), 2)
                context.entry_price = entry
                context.stop_price  = stop
                context.shadow_ledger.save(
                    context.ticker, context.qty, "INITIAL_ENTRY_Alpha_Trigger_V18.9",
                    entry_price=entry, stop_price=stop, state="OPEN"
                )
                log.info(f"[{context.ticker}] Entered @ ${entry:.2f}  Stop @ ${stop:.2f}")
                context.set_state(StateOpen())
        else:
            log.info(f"[{context.ticker}] No signal — holding off.")

class StateOpen(TradeState):
    def execute(self, context):
        price = get_price(context.ticker)
        entry = context.entry_price
        stop  = context.stop_price
        pnl   = (price - entry) / entry if entry else 0
        context.pnl = pnl

        log.info(f"[{context.ticker}] STATE: OPEN | Price ${price:.2f} | "
                 f"Entry ${entry:.2f} | PnL {pnl:+.1%} | Stop ${stop:.2f}")

        # Stop loss hit
        if price <= stop:
            log.warning(f"[{context.ticker}] STOP HIT @ ${price:.2f} — liquidating.")
            context.set_state(StateLiquidated())
            return

        # Profit gate: exit at 85%
        if pnl >= PROFIT_GATE:
            log.info(f"[{context.ticker}] PROFIT GATE {pnl:.1%} >= {PROFIT_GATE:.0%} — liquidating.")
            context.set_state(StateLiquidated())
            return

        # Breakeven: move stop to entry at 50% gain
        if pnl >= BREAKEVEN_PCT and stop < entry:
            context.adjust_stop_loss("BREAKEVEN", entry)

        # Check exit conditions (e.g. EMA crossover reversal)
        if context.check_exit_conditions():
            log.info(f"[{context.ticker}] Exit condition met — liquidating.")
            context.set_state(StateLiquidated())

class StateLiquidated(TradeState):
    def execute(self, context):
        context.close_all_positions()
        context.shadow_ledger.save(
            context.ticker, 0, "THESIS_COMPLETE_OR_KILL_SWITCH", state="LIQUIDATED"
        )
        log.info(f"[{context.ticker}] STATE: LIQUIDATED — agent complete.")
        context.running = False


# ══════════════════════════════════════════════════════════════════════════════
#  TRADING AGENT
# ══════════════════════════════════════════════════════════════════════════════

class TradingAgent:
    def __init__(self, ticker, qty=40, poll_secs=POLL_SECS):
        self.ticker      = ticker.upper()
        self.qty         = qty
        self.poll_secs   = poll_secs
        self.pnl         = 0.0
        self.entry_price = None
        self.stop_price  = None
        self.running     = True
        self.shadow_ledger = ShadowLedger(self.ticker)

        # Resume from ledger if position exists
        saved_state = self.shadow_ledger.get("state", "PENDING")
        if saved_state == "OPEN":
            self.entry_price = self.shadow_ledger.get("entry_price")
            self.stop_price  = self.shadow_ledger.get("stop_price")
            self.state = StateOpen()
            log.info(f"[{self.ticker}] Resuming OPEN state — entry ${self.entry_price} stop ${self.stop_price}")
        elif saved_state == "LIQUIDATED":
            log.info(f"[{self.ticker}] Already liquidated. Remove ledger file to restart.")
            self.running = False
            self.state = StateLiquidated()
        else:
            self.state = StatePending()

    def set_state(self, state):
        self.state = state

    # ── Signal: EMA20 > EMA50, price above both, RSI < 70 ───────────────────
    def check_signals(self):
        try:
            bars    = list(get_bars(self.ticker, 60))
            closes  = [float(b.close) for b in bars]
            if len(closes) < EMA_SLOW + 2:
                log.warning(f"[{self.ticker}] Not enough bars ({len(closes)}) for signal check.")
                return False
            price   = get_price(self.ticker)
            e20     = ema(closes, EMA_FAST)
            e50     = ema(closes, EMA_SLOW)
            rsi14   = rsi(closes, RSI_PERIOD)
            log.info(f"[{self.ticker}] Signal check — Price ${price:.2f} "
                     f"EMA20 ${e20} EMA50 ${e50} RSI {rsi14}")
            return price > e20 and price > e50 and e20 > e50 and rsi14 < RSI_OVERBOUGHT
        except Exception as e:
            log.error(f"[{self.ticker}] Signal check failed: {e}")
            return False

    # ── Exit: EMA20 crosses below EMA50 (trend reversal) ────────────────────
    def check_exit_conditions(self):
        try:
            bars   = list(get_bars(self.ticker, 60))
            closes = [float(b.close) for b in bars]
            if len(closes) < EMA_SLOW + 2:
                return False
            e20    = ema(closes, EMA_FAST)
            e50    = ema(closes, EMA_SLOW)
            if e20 < e50:
                log.info(f"[{self.ticker}] Exit: EMA20 ${e20} crossed below EMA50 ${e50}")
                return True
            return False
        except Exception as e:
            log.error(f"[{self.ticker}] Exit check failed: {e}")
            return False

    # ── Move stop loss ───────────────────────────────────────────────────────
    def adjust_stop_loss(self, reason, new_stop=None):
        if new_stop is None:
            new_stop = self.entry_price
        self.stop_price = round(new_stop, 2)
        self.shadow_ledger.save(
            self.ticker, self.qty, f"STOP_ADJUSTED_{reason}",
            stop_price=self.stop_price
        )
        log.info(f"[{self.ticker}] Stop adjusted to ${self.stop_price:.2f} ({reason})")

    # ── Close position ───────────────────────────────────────────────────────
    def close_all_positions(self):
        try:
            trading.close_position(self.ticker)
            log.info(f"[{self.ticker}] Position closed via Alpaca.")
        except Exception as e:
            log.error(f"[{self.ticker}] Failed to close position: {e}")

    # ── Place market order ───────────────────────────────────────────────────
    def place_order(self, qty, side):
        try:
            order = trading.submit_order(MarketOrderRequest(
                symbol=self.ticker, qty=qty, side=side, time_in_force=TimeInForce.DAY
            ))
            log.info(f"[{self.ticker}] Order placed: {side.value.upper()} {qty} shares — ID {order.id}")
            return order
        except Exception as e:
            log.error(f"[{self.ticker}] Order failed: {e}")
            return None

    # ── Main cycle ───────────────────────────────────────────────────────────
    def run_cycle(self):
        self.state.execute(self)

    # ── Event loop ───────────────────────────────────────────────────────────
    def run(self):
        acct = trading.get_account()
        log.info(f"═══ TradingAgent V18.9 | {self.ticker} | qty {self.qty} ═══")
        log.info(f"Alpaca: {acct.status} | equity ${float(acct.equity):,.2f} | cash ${float(acct.cash):,.2f}")

        while self.running:
            try:
                self.run_cycle()
            except Exception as e:
                log.error(f"[{self.ticker}] Cycle error: {e}")

            if self.running:
                log.info(f"[{self.ticker}] Sleeping {self.poll_secs}s...")
                time.sleep(self.poll_secs)

        log.info(f"[{self.ticker}] Agent stopped.")


# ══════════════════════════════════════════════════════════════════════════════
#  TICKERS.JSON LOADER
# ══════════════════════════════════════════════════════════════════════════════

TICKERS_FILE = _DIR / "tickers.json"

def load_tickers_config():
    """Load tickers from tickers.json. Returns list of dicts."""
    if not TICKERS_FILE.exists():
        log.warning(f"tickers.json not found at {TICKERS_FILE} — using NVDA default")
        return [{"symbol": "NVDA", "qty": 40}]
    with open(TICKERS_FILE) as f:
        data = json.load(f)
    return data.get("tickers", [])


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # CLI override: python trading_agent.py NVDA [qty]
    if len(sys.argv) > 1:
        ticker_list = [{"symbol": sys.argv[1].upper(),
                        "qty": int(sys.argv[2]) if len(sys.argv) > 2 else 40}]
        log.info(f"CLI override: monitoring {ticker_list[0]['symbol']}")
    else:
        ticker_list = load_tickers_config()
        log.info(f"Loaded {len(ticker_list)} ticker(s) from tickers.json: "
                 f"{[t['symbol'] for t in ticker_list]}")

    acct = trading.get_account()
    log.info(f"Alpaca: {acct.status} | equity ${float(acct.equity):,.2f} | cash ${float(acct.cash):,.2f}")

    if len(ticker_list) == 1:
        # Single ticker — run in main thread
        t = ticker_list[0]
        agent = TradingAgent(t["symbol"], t.get("qty", 40))
        agent.run()
    else:
        # Multiple tickers — each runs in its own thread
        threads = []
        for t in ticker_list:
            agent = TradingAgent(t["symbol"], t.get("qty", 40))
            thread = threading.Thread(
                target=agent.run,
                name=f"agent-{t['symbol']}",
                daemon=True,
            )
            threads.append(thread)
            thread.start()
            log.info(f"Started agent thread for {t['symbol']} (qty={t.get('qty', 40)})")
            time.sleep(1)  # stagger starts to avoid API rate limits

        log.info(f"All {len(threads)} agent threads running. Press Ctrl+C to stop.")
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(10)
        except KeyboardInterrupt:
            log.info("Shutdown signal received — agents will finish current cycle.")
