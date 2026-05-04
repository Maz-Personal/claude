"""
V18.9 Agentic Trading System — Full Options Spread Implementation
═════════════════════════════════════════════════════════════════════

ARCHITECTURE:
  State Machine: Pending → Open → [Reconcile] → Liquidated
                 Sandbox  ↗  (vol too high — holds until vol normalizes)

  NVDA: Bull Call Vertical  $197.5C / $202.5C  (Buy long / Sell short)
  XLE:  Bear Put Vertical   $60.0P  / $55.0P   (Buy long / Sell short)

  Expiry     : May 8, 2026
  Allocation : $20,000 per tranche
  Net Debit  : ~$1.42 (NVDA), ~$1.40 (XLE)
  Contracts  : 40 each

EXECUTION SCENARIOS:
  A (Optimal)     : B/A spread < 0.2% AND GK vol aligned → market order
  B (Conservative): Slippage > 0.5% → limit-chase (RECONCILE state)
  C (Thesis Break): NVDA < $192 OR XLE > $63.00 → immediate exit

SAFEGUARDS:
  - Friday Kill-Switch : force exit at 11:30 AM ET every Friday
  - Shadow Ledger      : local state synced against Alpaca every 30s
  - Leg-in lock        : prevents zombie positions on partial fills

Usage:
  python v18_agent.py              ← run with default config
  python v18_agent.py --dry-run    ← validate signals, no orders placed
"""

import json
import time
import math
import logging
import logging.handlers
import os
import sys
import threading
import argparse
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, ReplaceOrderRequest,
    GetOrdersRequest, GetOptionContractsRequest,
)
from alpaca.trading.enums import (
    OrderSide, TimeInForce, QueryOrderStatus,
    ContractType, ExerciseStyle, OrderStatus,
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# ── Paths & credentials ───────────────────────────────────────────────────────
_DIR = Path(__file__).parent
load_dotenv(_DIR.parent / ".env")

API_KEY    = os.getenv("WHEEL_ALPACA_API_KEY")
API_SECRET = os.getenv("WHEEL_ALPACA_API_SECRET")

trading = TradingClient(API_KEY, API_SECRET, paper=True)
mkt     = StockHistoricalDataClient(API_KEY, API_SECRET)

# ── Logging ───────────────────────────────────────────────────────────────────
_log_file = _DIR / "v18_agent.log"
_fh = logging.handlers.RotatingFileHandler(
    _log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_fh, _ch])
log = logging.getLogger(__name__)

# ── Strategy constants ────────────────────────────────────────────────────────
EXPIRY         = "2026-05-08"               # Options expiry (May 8)
EXPIRY_OCC     = "260508"                   # OCC date format for symbol

NVDA_LONG_STRIKE  = 197.50
NVDA_SHORT_STRIKE = 202.50
XLE_LONG_STRIKE   = 60.00
XLE_SHORT_STRIKE  = 55.00

QTY             = 40                        # Contracts per leg
PROFIT_GATE     = 0.85                      # Exit at 85% of max profit
BREAKEVEN_GATE  = 0.50                      # Move to breakeven at 50%
BA_SPREAD_MAX   = 0.002                     # 0.2% max bid/ask spread
SLIPPAGE_MAX    = 0.005                     # 0.5% slippage threshold
LIMIT_CHASE_MAX = 5                         # Max order mods per 60s window

NVDA_THESIS_BREAK = 192.00                  # Scenario C: NVDA below this → exit
XLE_THESIS_BREAK  = 63.00                   # Scenario C: XLE above this → exit

FRIDAY_KILL_HOUR  = 11                      # 11:30 AM ET kill-switch
FRIDAY_KILL_MIN   = 30

POLL_SECS       = 30                        # Main loop interval
LEDGER_SYNC_SECS = 30                       # Shadow ledger sync interval
GK_SHORT_WINDOW  = 1                        # GK vol: 1-min bars
GK_LONG_WINDOW   = 15                       # GK vol: 15-min smoothing


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def occ_symbol(ticker, strike, contract_type):
    """Build OCC options symbol. e.g. NVDA260508C00197500"""
    strike_int = int(round(strike * 1000))
    c = "C" if contract_type == ContractType.CALL else "P"
    return f"{ticker}{EXPIRY_OCC}{c}{strike_int:08d}"


def now_et():
    """Current time in US Eastern."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def is_friday_kill():
    """True if it's Friday at or after 11:30 AM ET."""
    t = now_et()
    return t.weekday() == 4 and (t.hour, t.minute) >= (FRIDAY_KILL_HOUR, FRIDAY_KILL_MIN)


def market_is_open():
    """Check if market is currently open via Alpaca clock."""
    try:
        clock = trading.get_clock()
        return clock.is_open
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  GARMAN-KLASS VOLATILITY
# ══════════════════════════════════════════════════════════════════════════════

class GarmanKlassVol:
    """
    Garman-Klass volatility estimator using OHLC bars.
    σ²_GK = 0.5·[ln(H/L)]² − (2·ln2 − 1)·[ln(C/O)]²
    Computes 1-min vol and 15-min EMA-smoothed vol.
    """
    _LN2_COEF = 2 * math.log(2) - 1

    def __init__(self, ticker):
        self.ticker   = ticker
        self.short_wn = GK_SHORT_WINDOW
        self.long_wn  = GK_LONG_WINDOW
        self._ema_vol = None

    def _gk_single(self, bar):
        try:
            o, h, l, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
            if o <= 0 or l <= 0: return 0.0
            return 0.5 * (math.log(h / l) ** 2) - self._LN2_COEF * (math.log(c / o) ** 2)
        except Exception:
            return 0.0

    def compute(self):
        """Returns (short_vol, smoothed_vol) — annualised."""
        try:
            start = datetime.now(timezone.utc) - timedelta(minutes=self.long_wn + 5)
            resp  = mkt.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=self.ticker,
                timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                start=start,
            ))
            bars = list(resp[self.ticker])
            if not bars:
                return 0.0, 0.0

            # Short window (1-min)
            short_var = self._gk_single(bars[-1]) * 252 * 6.5 * 60
            short_vol  = math.sqrt(max(short_var, 0))

            # Smoothed (EMA over long_wn bars)
            k = 2 / (self.long_wn + 1)
            if self._ema_vol is None:
                self._ema_vol = short_vol
            else:
                self._ema_vol = short_vol * k + self._ema_vol * (1 - k)

            return round(short_vol, 4), round(self._ema_vol, 4)
        except Exception as e:
            log.warning(f"GK vol failed for {self.ticker}: {e}")
            return 0.0, 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  OPTIONS QUOTE + SPREAD CHECK
# ══════════════════════════════════════════════════════════════════════════════

def get_option_quote(symbol):
    """
    Fetch bid/ask for an options contract via Alpaca.
    Returns (bid, ask, mid) or None on failure.
    """
    try:
        contracts = trading.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[symbol[:4] if symbol[:4].isalpha() else symbol.rstrip('0123456789CP')],
            expiration_date=EXPIRY,
        ))
        # Find exact contract
        match = next((c for c in contracts.option_contracts
                      if c.symbol == symbol), None)
        if not match:
            log.warning(f"Contract not found: {symbol}")
            return None
        # Use close price as proxy (paper trading often has no live options quotes)
        if match.close_price:
            mid = float(match.close_price)
            return mid * 0.98, mid * 1.02, mid   # synthetic bid/ask ±2%
        return None
    except Exception as e:
        log.warning(f"Option quote failed for {symbol}: {e}")
        return None


def get_underlying_quote(ticker):
    """Bid/ask for the underlying stock."""
    try:
        resp = mkt.get_stock_latest_quote(StockLatestQuoteRequest(
            symbol_or_symbols=ticker
        ))
        q = resp[ticker]
        return float(q.bid_price), float(q.ask_price)
    except Exception as e:
        log.warning(f"Quote failed for {ticker}: {e}")
        return None, None


def ba_spread_pct(bid, ask):
    """Bid/ask spread as fraction of mid."""
    if not bid or not ask or (bid + ask) == 0:
        return 1.0
    return (ask - bid) / ((ask + bid) / 2)


# ══════════════════════════════════════════════════════════════════════════════
#  SHADOW LEDGER — V18.9 with Alpaca Remote Sync
# ══════════════════════════════════════════════════════════════════════════════

class ShadowLedger:
    """
    Persists local position state and cross-checks against Alpaca every 30s.
    Prevents ghost executions and zombie positions.
    """
    def __init__(self, filename="v18_shadow_ledger.json"):
        self.filename = _DIR / filename
        self.state    = self._load()
        self._lock    = threading.Lock()
        self._running = True
        self._thread  = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()

    def _load(self):
        if self.filename.exists():
            with open(self.filename) as f:
                return json.load(f)
        return {
            "positions": {},
            "orders":    {},
            "agent_state": "PENDING",
            "entry_debit": {},
            "last_sync":   None,
            "last_alpaca_check": None,
        }

    def save(self, key=None, value=None, agent_state=None):
        with self._lock:
            if key and value is not None:
                self.state["positions"][key] = value
            if agent_state:
                self.state["agent_state"] = agent_state
            self.state["last_sync"] = datetime.now(timezone.utc).isoformat()
            with open(self.filename, "w") as f:
                json.dump(self.state, f, indent=2)

    def record_order(self, leg_name, order_id, symbol, qty, side):
        with self._lock:
            self.state["orders"][leg_name] = {
                "order_id": order_id,
                "symbol": symbol,
                "qty": qty,
                "side": side,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(self.filename, "w") as f:
                json.dump(self.state, f, indent=2)

    def get(self, key, default=None):
        return self.state.get(key, default)

    def _sync_loop(self):
        """Background thread: compare local ledger vs Alpaca positions every 30s."""
        while self._running:
            time.sleep(LEDGER_SYNC_SECS)
            try:
                self._alpaca_sync()
            except Exception as e:
                log.warning(f"Ledger sync error: {e}")

    def _alpaca_sync(self):
        positions = {p.symbol: int(p.qty) for p in trading.get_all_positions()}
        with self._lock:
            self.state["last_alpaca_check"] = datetime.now(timezone.utc).isoformat()
            for sym, local_qty in self.state["positions"].items():
                remote_qty = positions.get(sym, 0)
                if local_qty != remote_qty:
                    log.warning(
                        f"LEDGER DRIFT: {sym} local={local_qty} alpaca={remote_qty} — correcting."
                    )
                    self.state["positions"][sym] = remote_qty
            with open(self.filename, "w") as f:
                json.dump(self.state, f, indent=2)
        log.info("Ledger synced with Alpaca.")

    def clear_pending(self, ticker):
        """Purge any pending position state for a given ticker (e.g. old USO cache)."""
        with self._lock:
            removed = {k: v for k, v in self.state["positions"].items() if ticker.upper() in k}
            for k in removed:
                del self.state["positions"][k]
            removed_orders = {k: v for k, v in self.state["orders"].items()
                              if ticker.upper() in v.get("symbol", "")}
            for k in removed_orders:
                del self.state["orders"][k]
            if removed or removed_orders:
                log.info(f"Ledger: cleared {len(removed)} position(s) and "
                         f"{len(removed_orders)} order(s) for {ticker}")
            with open(self.filename, "w") as f:
                json.dump(self.state, f, indent=2)

    def initialize_session(self, ticker):
        """Mark a fresh session for a ticker in the ledger."""
        with self._lock:
            self.state.setdefault("sessions", {})[ticker] = {
                "initialized_at": datetime.now(timezone.utc).isoformat(),
                "status": "ACTIVE",
            }
            log.info(f"Ledger: session initialized for {ticker}")
            with open(self.filename, "w") as f:
                json.dump(self.state, f, indent=2)

    def stop(self):
        self._running = False


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

class LegExecution:
    """
    Handles placing, limit-chasing, and cancelling a single options leg.
    Scenario B: Modify limit up to LIMIT_CHASE_MAX times per 60s window.
    """
    def __init__(self, symbol, qty, side, dry_run=False):
        self.symbol   = symbol
        self.qty      = qty
        self.side     = side
        self.dry_run  = dry_run
        self.order_id = None
        self.filled   = False

    def place_market(self):
        if self.dry_run:
            log.info(f"  [DRY-RUN] MARKET {self.side.value.upper()} {self.qty}x {self.symbol}")
            self.filled = True
            return True
        try:
            order = trading.submit_order(MarketOrderRequest(
                symbol=self.symbol,
                qty=self.qty,
                side=self.side,
                time_in_force=TimeInForce.DAY,
            ))
            self.order_id = str(order.id)
            log.info(f"  MARKET {self.side.value.upper()} {self.qty}x {self.symbol} → {self.order_id}")
            return True
        except Exception as e:
            log.error(f"  Market order failed {self.symbol}: {e}")
            return False

    def place_limit(self, limit_price):
        if self.dry_run:
            log.info(f"  [DRY-RUN] LIMIT {self.side.value.upper()} {self.qty}x {self.symbol} @ ${limit_price:.2f}")
            self.filled = True
            return True
        try:
            order = trading.submit_order(LimitOrderRequest(
                symbol=self.symbol,
                qty=self.qty,
                side=self.side,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
            ))
            self.order_id = str(order.id)
            log.info(f"  LIMIT {self.side.value.upper()} {self.qty}x {self.symbol} @ ${limit_price:.2f} → {self.order_id}")
            return True
        except Exception as e:
            log.error(f"  Limit order failed {self.symbol}: {e}")
            return False

    def chase_limit(self, new_price):
        """Modify existing limit order to new_price (Scenario B limit-chasing)."""
        if self.dry_run or not self.order_id:
            return
        try:
            trading.replace_order_by_id(self.order_id, ReplaceOrderRequest(
                limit_price=round(new_price, 2)
            ))
            log.info(f"  LIMIT-CHASE {self.symbol} → ${new_price:.2f}")
        except Exception as e:
            log.warning(f"  Chase failed {self.symbol}: {e}")

    def cancel(self):
        if self.dry_run or not self.order_id:
            return
        try:
            trading.cancel_order_by_id(self.order_id)
            log.info(f"  Cancelled order {self.order_id}")
        except Exception as e:
            log.warning(f"  Cancel failed {self.order_id}: {e}")

    def check_filled(self):
        if self.filled or self.dry_run:
            return True
        if not self.order_id:
            return False
        try:
            order = trading.get_order_by_id(self.order_id)
            if order.status == OrderStatus.FILLED:
                self.filled = True
                return True
        except Exception:
            pass
        return False

    def close(self):
        """Close this leg with a market order in the opposite direction."""
        if self.dry_run:
            log.info(f"  [DRY-RUN] CLOSE {self.symbol}")
            return
        close_side = OrderSide.SELL if self.side == OrderSide.BUY else OrderSide.BUY
        try:
            trading.submit_order(MarketOrderRequest(
                symbol=self.symbol,
                qty=self.qty,
                side=close_side,
                time_in_force=TimeInForce.DAY,
            ))
            log.info(f"  CLOSE {self.symbol} — {close_side.value.upper()} {self.qty}")
        except Exception as e:
            log.error(f"  Close failed {self.symbol}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════════

class TradeState(ABC):
    @abstractmethod
    def execute(self, ctx): pass


class StatePending(TradeState):
    """Wait for entry signal. Check scenarios A/B/C before entering."""

    def execute(self, ctx):
        log.info("STATE: PENDING — evaluating entry conditions...")

        # Scenario C check first — thesis break
        if ctx.thesis_broken():
            log.warning("Scenario C: Thesis broken before entry — staying out.")
            return

        # Friday kill-switch
        if is_friday_kill():
            log.warning("Friday kill-switch active — no new entries.")
            return

        # Market must be open
        if not market_is_open() and not ctx.dry_run:
            log.info("Market closed — skipping entry check.")
            return

        # Get underlying prices
        nvda_bid, nvda_ask = get_underlying_quote("NVDA")
        xle_bid,  xle_ask  = get_underlying_quote("XLE")

        if not nvda_bid or not xle_bid:
            log.warning("Could not fetch quotes — skipping.")
            return

        nvda_price = (nvda_bid + nvda_ask) / 2
        xle_price  = (xle_bid  + xle_ask)  / 2
        log.info(f"NVDA ${nvda_price:.2f}  XLE ${xle_price:.2f}")

        # GK Volatility alignment check
        nvda_short_vol, nvda_smooth_vol = ctx.gk_nvda.compute()
        log.info(f"NVDA GK Vol: 1-min={nvda_short_vol:.4f}  15-min={nvda_smooth_vol:.4f}")
        vol_aligned = abs(nvda_short_vol - nvda_smooth_vol) / max(nvda_smooth_vol, 0.0001) < 0.20

        # B/A spread check on options legs
        nvda_long_sym  = occ_symbol("NVDA", NVDA_LONG_STRIKE,  ContractType.CALL)
        nvda_short_sym = occ_symbol("NVDA", NVDA_SHORT_STRIKE, ContractType.CALL)
        xle_long_sym   = occ_symbol("XLE",  XLE_LONG_STRIKE,   ContractType.PUT)
        xle_short_sym  = occ_symbol("XLE",  XLE_SHORT_STRIKE,  ContractType.PUT)

        log.info(f"Option symbols: {nvda_long_sym} / {nvda_short_sym} / {xle_long_sym} / {xle_short_sym}")

        nvda_long_q  = get_option_quote(nvda_long_sym)
        nvda_short_q = get_option_quote(nvda_short_sym)
        xle_long_q   = get_option_quote(xle_long_sym)
        xle_short_q  = get_option_quote(xle_short_sym)

        # Determine scenario
        spreads_ok = True
        for q, sym in [(nvda_long_q, nvda_long_sym), (nvda_short_q, nvda_short_sym),
                       (xle_long_q,  xle_long_sym),  (xle_short_q,  xle_short_sym)]:
            if q:
                sp = ba_spread_pct(q[0], q[1])
                log.info(f"  {sym}: bid={q[0]:.2f} ask={q[1]:.2f} spread={sp:.3%}")
                if sp > BA_SPREAD_MAX:
                    spreads_ok = False
            else:
                spreads_ok = False  # can't verify — treat as wide

        if spreads_ok and vol_aligned:
            log.info("Scenario A (OPTIMAL): tight spreads + vol aligned → MARKET entry.")
            ctx._enter(
                nvda_long_sym, nvda_short_sym, xle_long_sym, xle_short_sym,
                use_limit=False
            )
        else:
            log.info("Scenario B (CONSERVATIVE): wide spreads or vol misaligned → LIMIT entry.")
            # Use mid price as limit
            def mid(q): return q[2] if q else None
            ctx._enter(
                nvda_long_sym, nvda_short_sym, xle_long_sym, xle_short_sym,
                use_limit=True,
                limits={
                    nvda_long_sym:  mid(nvda_long_q)  or NVDA_LONG_STRIKE  * 0.01,
                    nvda_short_sym: mid(nvda_short_q) or NVDA_SHORT_STRIKE * 0.01,
                    xle_long_sym:   mid(xle_long_q)   or XLE_LONG_STRIKE   * 0.01,
                    xle_short_sym:  mid(xle_short_q)  or XLE_SHORT_STRIKE  * 0.01,
                }
            )


class StateSandbox(TradeState):
    """
    State 3 — SANDBOX: Vol too high to enter safely.
    1-min GK vol > 2x 15-min smoothed vol → hold until vol normalises.
    Re-checks every poll cycle and transitions to PENDING when safe.
    """
    def execute(self, ctx):
        log.info("STATE: SANDBOX — elevated vol detected, monitoring for normalisation...")
        short_vol, smooth_vol = ctx.get_gk_vol("1min"), ctx.get_gk_vol("15min")
        log.info(f"  GK Vol — 1-min: {short_vol:.4f}  15-min: {smooth_vol:.4f}  "
                 f"ratio: {short_vol / max(smooth_vol, 0.0001):.2f}x")
        if smooth_vol > 0 and short_vol <= 2 * smooth_vol:
            log.info("SANDBOX → Vol normalised. Transitioning to PENDING.")
            ctx.transition_to("PENDING")
        else:
            log.info("SANDBOX → Vol still elevated. Holding.")


class StateOpen(TradeState):
    """Monitor open spread positions. Check profit gate, stop, Greek drift."""

    def execute(self, ctx):
        log.info("STATE: OPEN — monitoring positions...")

        # Friday kill-switch
        if is_friday_kill():
            log.warning("Friday kill-switch (11:30 AM ET) — forcing liquidation.")
            ctx.set_state(StateLiquidated("FRIDAY_KILL_SWITCH"))
            return

        # Scenario C: thesis break
        if ctx.thesis_broken():
            log.warning("Scenario C: Thesis broken — liquidating immediately.")
            ctx.set_state(StateLiquidated("THESIS_BREAK"))
            return

        # PnL calculation
        pnl = ctx.compute_pnl()
        log.info(f"Current PnL: {pnl:+.1%}")

        if pnl >= PROFIT_GATE:
            log.info(f"Profit gate hit ({pnl:.1%} >= {PROFIT_GATE:.0%}) — liquidating.")
            ctx.set_state(StateLiquidated("PROFIT_GATE"))
            return

        if pnl >= BREAKEVEN_GATE and not ctx.at_breakeven:
            log.info(f"Breakeven gate ({pnl:.1%}) — recording breakeven mark.")
            ctx.at_breakeven = True
            ctx.ledger.save(agent_state="OPEN_BREAKEVEN")

        # Check if any limit orders need chasing (Scenario B fallthrough)
        unfilled = [leg for leg in ctx.legs.values() if not leg.check_filled()]
        if unfilled:
            ctx.set_state(StateReconcile(unfilled))


class StateReconcile(TradeState):
    """
    Scenario B: Limit-chasing mode.
    Modifies unfilled limit orders up to 5x per 60s window.
    """
    def __init__(self, unfilled_legs):
        self.unfilled   = unfilled_legs
        self._mods      = 0
        self._window_start = time.time()

    def execute(self, ctx):
        log.info(f"STATE: RECONCILE — {len(self.unfilled)} unfilled leg(s).")

        # Reset mod counter every 60s
        if time.time() - self._window_start > 60:
            self._mods = 0
            self._window_start = time.time()

        if self._mods >= LIMIT_CHASE_MAX:
            log.warning("RECONCILE: Max limit chases reached this window — waiting.")
            return

        for leg in self.unfilled:
            if leg.check_filled():
                self.unfilled.remove(leg)
                continue
            # Chase limit up by 1 tick (0.01) toward market
            q = get_option_quote(leg.symbol)
            if q:
                new_price = round(q[1] * 1.005, 2)  # ask + 0.5% nudge
                leg.chase_limit(new_price)
                self._mods += 1

        if not self.unfilled:
            log.info("RECONCILE: All legs filled — transitioning to OPEN.")
            ctx.set_state(StateOpen())
        elif is_friday_kill():
            log.warning("RECONCILE: Friday kill-switch — cancelling and liquidating.")
            for leg in self.unfilled:
                leg.cancel()
            ctx.set_state(StateLiquidated("FRIDAY_KILL_RECONCILE"))


class StateLiquidated(TradeState):
    """Close all legs and update shadow ledger."""
    def __init__(self, reason="THESIS_COMPLETE"):
        self.reason = reason

    def execute(self, ctx):
        log.info(f"STATE: LIQUIDATED ({self.reason}) — closing all legs.")
        for name, leg in ctx.legs.items():
            log.info(f"  Closing leg: {name}")
            leg.close()
        ctx.ledger.save(agent_state=f"LIQUIDATED_{self.reason}")
        log.info("All legs closed. Shadow ledger updated. Agent complete.")
        ctx.running = False


# ══════════════════════════════════════════════════════════════════════════════
#  V18 AGENT
# ══════════════════════════════════════════════════════════════════════════════

class V18Agent:
    def __init__(self,
                 nvda_strikes=(NVDA_LONG_STRIKE, NVDA_SHORT_STRIKE),
                 xle_strikes=(XLE_LONG_STRIKE, XLE_SHORT_STRIKE),
                 allocation=20000,
                 qty=QTY,
                 dry_run=False):
        self.nvda_long_strike  = nvda_strikes[0]
        self.nvda_short_strike = nvda_strikes[1]
        self.xle_long_strike   = xle_strikes[0]
        self.xle_short_strike  = xle_strikes[1]
        self.allocation        = allocation
        self.qty               = qty
        self.dry_run           = dry_run
        self.running           = True
        self.legs              = {}
        self.at_breakeven      = False
        self.entry_debits      = {}
        self.ledger            = ShadowLedger()
        self.gk_nvda           = GarmanKlassVol("NVDA")

        # Purge any stale USO state and initialise XLE session
        self.ledger.clear_pending("USO")
        self.ledger.initialize_session("XLE")

        # Resume or initialise state
        saved = self.ledger.get("agent_state", "PENDING")
        if "LIQUIDATED" in saved:
            log.info("Agent already liquidated. Delete ledger file to restart.")
            self.running = False
        elif saved in ("OPEN", "OPEN_BREAKEVEN"):
            log.info(f"Resuming from saved state: {saved}")
            self.state = StateOpen()
            self.at_breakeven = saved == "OPEN_BREAKEVEN"
        else:
            # V18.9 RE-INIT: check vol regime before entering PENDING
            short_vol  = self.get_gk_vol("1min")
            smooth_vol = self.get_gk_vol("15min")
            if smooth_vol > 0 and short_vol > 2 * smooth_vol:
                log.warning(f"GK Vol elevated ({short_vol:.4f} > 2x {smooth_vol:.4f}) — starting in SANDBOX.")
                self.transition_to("SANDBOX")
            else:
                self.transition_to("PENDING")

    def set_state(self, state):
        log.info(f"→ Transitioning to {type(state).__name__}")
        self.state = state

    def transition_to(self, state_name):
        """Named state transitions — matches pseudocode interface."""
        states = {
            "PENDING":    StatePending,
            "OPEN":       StateOpen,
            "SANDBOX":    StateSandbox,
            "RECONCILE":  lambda: StateReconcile([]),
            "LIQUIDATED": lambda: StateLiquidated("MANUAL"),
        }
        factory = states.get(state_name.upper())
        if not factory:
            log.error(f"Unknown state: {state_name}")
            return
        self.set_state(factory())
        self.ledger.save(agent_state=state_name.upper())

    def get_gk_vol(self, latency="15min"):
        """
        Returns GK volatility for the given latency window.
        latency: '1min' → short window, '15min' → smoothed EMA
        """
        short_vol, smooth_vol = self.gk_nvda.compute()
        return short_vol if latency == "1min" else smooth_vol

    def thesis_broken(self):
        """Scenario C: NVDA < $192 OR XLE > $148.50."""
        try:
            nvda_bid, nvda_ask = get_underlying_quote("NVDA")
            xle_bid,  xle_ask  = get_underlying_quote("XLE")
            nvda_price = (nvda_bid + nvda_ask) / 2 if nvda_bid else 999
            xle_price  = (xle_bid  + xle_ask)  / 2 if xle_bid  else 0
            broken = nvda_price < NVDA_THESIS_BREAK or xle_price > XLE_THESIS_BREAK
            if broken:
                log.warning(f"Thesis broken: NVDA=${nvda_price:.2f} XLE=${xle_price:.2f}")
            return broken
        except Exception:
            return False

    def compute_pnl(self):
        """
        Estimate PnL as fraction of net debit paid.
        Net debit = sum(BUY prices) - sum(SELL prices) at entry.
        PnL = (current net value - entry net debit) / |entry net debit|
        """
        try:
            if not self.legs or not self.entry_debits:
                return 0.0
            net_entry   = 0.0
            net_current = 0.0
            legs_priced = 0
            for name, leg in self.legs.items():
                entry_price = self.entry_debits.get(leg.symbol)
                if entry_price is None:
                    continue
                q = get_option_quote(leg.symbol)
                if not q:
                    log.warning(f"PnL: no quote for {leg.symbol} — skipping.")
                    continue
                sign = 1 if leg.side == OrderSide.BUY else -1
                net_entry   += entry_price * sign   # BUY = paid, SELL = received
                net_current += q[2]        * sign   # current value
                legs_priced += 1
            if legs_priced == 0 or abs(net_entry) < 0.001:
                return 0.0
            pnl = (net_current - net_entry) / abs(net_entry)
            log.info(f"PnL detail: net_entry={net_entry:.4f}  net_current={net_current:.4f}  pnl={pnl:+.1%}")
            return pnl
        except Exception as e:
            log.warning(f"PnL computation failed: {e}")
            return 0.0

    def _enter(self, nvda_long_sym, nvda_short_sym, xle_long_sym, xle_short_sym,
               use_limit=False, limits=None):
        """Place all four legs. Sets legs dict and transitions to StateOpen."""
        log.info("=== ENTERING POSITION ===")
        specs = [
            ("nvda_long",  nvda_long_sym,  OrderSide.BUY),
            ("nvda_short", nvda_short_sym, OrderSide.SELL),
            ("xle_long",   xle_long_sym,   OrderSide.BUY),
            ("xle_short",  xle_short_sym,  OrderSide.SELL),
        ]
        all_ok = True
        for name, sym, side in specs:
            leg = LegExecution(sym, QTY, side, dry_run=self.dry_run)
            if use_limit and limits and sym in limits:
                ok = leg.place_limit(limits[sym])
            else:
                ok = leg.place_market()
            self.legs[name] = leg
            self.ledger.record_order(name, leg.order_id or "DRY_RUN", sym, QTY, side.value)
            if ok and limits:
                self.entry_debits[sym] = limits.get(sym, 0)  # raw price, sign applied in compute_pnl
            if not ok:
                all_ok = False

        if all_ok:
            self.ledger.save(agent_state="OPEN")
            self.set_state(StateOpen())
        else:
            log.error("One or more legs failed — cancelling all and staying PENDING.")
            for leg in self.legs.values():
                leg.cancel()
            self.legs.clear()

    def run(self):
        acct = trading.get_account()
        log.info("═══════════════════════════════════════════════════")
        log.info("  V18.9 Agentic System | Options Spread Agent")
        log.info(f"  Mode    : {'DRY-RUN' if self.dry_run else 'LIVE (PAPER)'}")
        log.info(f"  NVDA    : Bull Call Vertical ${NVDA_LONG_STRIKE}C / ${NVDA_SHORT_STRIKE}C  x{QTY}")
        log.info(f"  XLE     : Bear Put Vertical  ${XLE_LONG_STRIKE}P / ${XLE_SHORT_STRIKE}P  x{QTY}")
        log.info(f"  Expiry  : {EXPIRY}")
        log.info(f"  Account : equity ${float(acct.equity):,.2f}  cash ${float(acct.cash):,.2f}")
        log.info("═══════════════════════════════════════════════════")

        while self.running:
            try:
                self.state.execute(self)
            except Exception as e:
                log.error(f"Cycle error: {e}", exc_info=True)

            if self.running:
                log.info(f"Sleeping {POLL_SECS}s...")
                time.sleep(POLL_SECS)

        self.ledger.stop()
        log.info("V18.9 Agent stopped.")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V18.9 Options Spread Agent")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate signals and logic without placing orders")
    args = parser.parse_args()

    # V18.9 RE-INITIALIZATION — XLE swap, configurable strikes
    nvda_agent = V18Agent(
        nvda_strikes=(197.50, 202.50),   # Bull Call Vertical
        xle_strikes=(60.00, 55.00),      # Bear Put Vertical (XLE @ ~$59)
        allocation=20000,                # $20k per tranche
        qty=40,
        dry_run=args.dry_run,
    )
    nvda_agent.run()
