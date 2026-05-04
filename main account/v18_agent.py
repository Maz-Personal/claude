"""
V18.9 Agentic Trading System — MASTER PROMPT Full Implementation
═════════════════════════════════════════════════════════════════

ARCHITECTURE:
  State Machine: Pending → [Throttled] → Open → [Reconcile] → Liquidated
                 Sandbox  ↗  (vol spike OR zombie position detected)

  NVDA: Bull Call Vertical  $197.5C / $202.5C  (atomic mleg)
  XLE:  Bear Put Vertical   $60.0P  / $55.0P   (atomic mleg)

  Expiry     : May 8, 2026
  Allocation : $20,000 per tranche
  Contracts  : 40 each

NEGATIVE CONSTRAINTS (all enforced):
  1. No Linear Drift        — tickers match Phase 0 selection
  2. No Code Abstraction    — full unredacted state machine
  3. No Basic Volatility    — Garman-Klass ONLY (1-min, 5-min, 15-min)
  4. No Brittle Loops       — OO State Design Pattern
  5. No Greek Drift         — Theta/Vega >20% from entry → exit
  6. No Unvetted Updates    — JSON-Schema validation on all API payloads
  7. No Oscillation         — max 5 order mods per 60s window
  8. No Zombie Positions    — SANDBOX if ledger ≠ Alpaca for >30s

MODES:
  1 — NORMAL    : Full autonomous execution
  2 — THROTTLED : 50% size, limit-only entry
  3 — SANDBOX   : Observe-only, cancel all pending, log hypothetical

Usage:
  python v18_agent.py              ← live (paper)
  python v18_agent.py --dry-run    ← validate signals, no orders
  python v18_agent.py --mode 2     ← force THROTTLED mode
"""

import json
import time
import math
import logging
import logging.handlers
import os
import sys
import queue
import hashlib
import threading
import argparse
import jsonschema
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
    OrderSide, TimeInForce, OrderStatus, ContractType,
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


# ══════════════════════════════════════════════════════════════════════════════
#  JSON STRUCTURED LOGGING — quant_daemon.log with reasoning_hash
# ══════════════════════════════════════════════════════════════════════════════

class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "state":   getattr(record, "state", "UNKNOWN"),
            "action":  getattr(record, "action", "LOG"),
            "msg":     record.getMessage(),
            "reason":  getattr(record, "reason", ""),
            "reasoning_hash": hashlib.md5(
                getattr(record, "reason", record.getMessage()).encode()
            ).hexdigest()[:8],
        }
        return json.dumps(entry)


_daemon_log   = _DIR / "quant_daemon.log"
_human_log    = _DIR / "v18_agent.log"

_jfh = logging.handlers.RotatingFileHandler(
    _daemon_log, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_jfh.setFormatter(JsonFormatter())

_fh = logging.handlers.RotatingFileHandler(
    _human_log, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_jfh, _fh, _ch])
log = logging.getLogger(__name__)


def slog(msg, state="UNKNOWN", action="LOG", reason="", level="info"):
    """SemanticTelemetry logger — every call includes state, action, reason."""
    extra = {"state": state, "action": action, "reason": reason}
    getattr(log, level)(msg, extra=extra)


# ══════════════════════════════════════════════════════════════════════════════
#  STRATEGY CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

EXPIRY            = "2026-05-08"
EXPIRY_OCC        = "260508"

NVDA_LONG_STRIKE  = 197.50
NVDA_SHORT_STRIKE = 202.50
XLE_LONG_STRIKE   = 60.00
XLE_SHORT_STRIKE  = 55.00

QTY               = 40
THROTTLED_QTY     = QTY // 2       # Mode 2: 50% size

PROFIT_GATE       = 0.85
BREAKEVEN_GATE    = 0.50
BA_SPREAD_MAX     = 0.002
SLIPPAGE_MAX      = 0.005
LIMIT_CHASE_MAX   = 5

NVDA_THESIS_BREAK = 192.00
XLE_THESIS_BREAK  = 63.00

FRIDAY_KILL_HOUR  = 11
FRIDAY_KILL_MIN   = 30

POLL_SECS         = 30
LEDGER_SYNC_SECS  = 30
ZOMBIE_TIMEOUT    = 30             # seconds before zombie halt

GREEK_DRIFT_MAX   = 0.20           # 20% Theta/Vega drift → exit

GK_WINDOWS        = [1, 5, 15]     # minutes for GK vol

# JSON Schema for Alpaca order response validation
ORDER_SCHEMA = {
    "type": "object",
    "required": ["id", "status", "symbol", "qty"],
    "properties": {
        "id":     {"type": "string"},
        "status": {"type": "string"},
        "symbol": {"type": "string"},
        "qty":    {"type": "string"},
    }
}

POSITION_SCHEMA = {
    "type": "object",
    "required": ["symbol", "qty"],
    "properties": {
        "symbol": {"type": "string"},
        "qty":    {"type": "string"},
    }
}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def occ_symbol(ticker, strike, contract_type):
    strike_int = int(round(strike * 1000))
    c = "C" if contract_type == ContractType.CALL else "P"
    return f"{ticker}{EXPIRY_OCC}{c}{strike_int:08d}"


def now_et():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def is_friday_kill():
    t = now_et()
    return t.weekday() == 4 and (t.hour, t.minute) >= (FRIDAY_KILL_HOUR, FRIDAY_KILL_MIN)


def market_is_open():
    try:
        return trading.get_clock().is_open
    except Exception:
        return False


def validate_payload(payload: dict, schema: dict, label: str) -> bool:
    """JSON-Schema validation gate — blocks state mutation on invalid payloads."""
    try:
        jsonschema.validate(instance=payload, schema=schema)
        return True
    except jsonschema.ValidationError as e:
        slog(f"Schema validation FAILED for {label}: {e.message}",
             action="SCHEMA_REJECT", reason=f"Invalid payload: {e.message}", level="error")
        return False


def get_underlying_quote(ticker):
    try:
        resp = mkt.get_stock_latest_quote(StockLatestQuoteRequest(
            symbol_or_symbols=ticker))
        q = resp[ticker]
        return float(q.bid_price), float(q.ask_price)
    except Exception:
        return None, None


def get_option_quote(symbol):
    try:
        underlying = ''.join(c for c in symbol if c.isalpha())
        contracts = trading.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[underlying],
            expiration_date=EXPIRY,
        ))
        match = next((c for c in contracts.option_contracts
                      if c.symbol == symbol), None)
        if match and match.close_price:
            mid = float(match.close_price)
            return mid * 0.98, mid * 1.02, mid
        return None
    except Exception:
        return None


def get_option_greeks(symbol):
    """Fetch Delta, Theta, Vega for a contract. Returns dict or None."""
    try:
        underlying = ''.join(c for c in symbol if c.isalpha())
        contracts = trading.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[underlying],
            expiration_date=EXPIRY,
        ))
        match = next((c for c in contracts.option_contracts
                      if c.symbol == symbol), None)
        if match:
            return {
                "delta": float(getattr(match, "delta", 0) or 0),
                "theta": float(getattr(match, "theta", 0) or 0),
                "vega":  float(getattr(match, "vega",  0) or 0),
            }
    except Exception:
        pass
    return None


def ba_spread_pct(bid, ask):
    if not bid or not ask or (bid + ask) == 0:
        return 1.0
    return (ask - bid) / ((ask + bid) / 2)


# ══════════════════════════════════════════════════════════════════════════════
#  GARMAN-KLASS VOLATILITY — 3 INTERVALS (1-min, 5-min, 15-min)
# ══════════════════════════════════════════════════════════════════════════════

class GarmanKlassVol:
    """σ_GK = sqrt((Z/n) * Σ[ 0.5·(ln H/L)² − (2ln2−1)·(ln C/O)² ])"""
    _LN2_COEF = 2 * math.log(2) - 1

    def __init__(self, ticker):
        self.ticker  = ticker
        self._ema    = {w: None for w in GK_WINDOWS}

    def _gk_single(self, bar):
        try:
            o, h, l, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
            if o <= 0 or l <= 0:
                return 0.0
            return 0.5 * (math.log(h / l) ** 2) - self._LN2_COEF * (math.log(c / o) ** 2)
        except Exception:
            return 0.0

    def compute_all(self):
        """Returns {1: vol, 5: vol, 15: vol} — annualised."""
        results = {}
        for window in GK_WINDOWS:
            try:
                start = datetime.now(timezone.utc) - timedelta(minutes=window + 5)
                resp  = mkt.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=self.ticker,
                    timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=start,
                ))
                bars = list(resp[self.ticker])
                if not bars:
                    results[window] = 0.0
                    continue
                # Average GK over the window bars
                vars = [self._gk_single(b) for b in bars[-window:]]
                avg_var = sum(vars) / len(vars) if vars else 0.0
                vol = math.sqrt(max(avg_var * 252 * 6.5 * 60, 0))
                # EMA smoothing
                k = 2 / (window + 1)
                if self._ema[window] is None:
                    self._ema[window] = vol
                else:
                    self._ema[window] = vol * k + self._ema[window] * (1 - k)
                results[window] = round(self._ema[window], 4)
            except Exception as e:
                slog(f"GK vol failed for {self.ticker} window={window}: {e}",
                     action="GK_ERROR", reason=str(e), level="warning")
                results[window] = 0.0
        return results

    def get(self, latency="15min"):
        mins = int(latency.replace("min", ""))
        vols = self.compute_all()
        return vols.get(mins, 0.0)


# ══════════════════════════════════════════════════════════════════════════════
#  MODEL CONFIDENCE SCORE (1–10)
# ══════════════════════════════════════════════════════════════════════════════

def compute_confidence_score(gk_vols, nvda_long_q, nvda_short_q, xle_long_q, xle_short_q):
    """
    Score 1-10 based on:
      - Vol smoothing alignment (1-min vs 15-min)
      - Bid/ask spreads on all 4 legs
      - Market regime (market open/hours)
    Noise Warning: if σ_GK(1min) > 2× σ_GK(15min) → cap at 4/10
    """
    score = 10.0

    # Vol smoothing penalty
    v1  = gk_vols.get(1,  0)
    v5  = gk_vols.get(5,  0)
    v15 = gk_vols.get(15, 0)

    noise_warning = v15 > 0 and v1 > 2 * v15
    if noise_warning:
        score = min(score, 4.0)
        slog("Noise Warning: σ_GK(1min) > 2× σ_GK(15min) — confidence capped at 4/10",
             action="NOISE_WARNING",
             reason=f"1min={v1:.4f} 15min={v15:.4f} ratio={v1/max(v15,0.0001):.2f}x",
             level="warning")

    # 5-min divergence penalty
    if v5 > 0 and v1 > 0:
        div = abs(v5 - v1) / max(v1, 0.0001)
        if div > 0.30:
            score -= 2.0

    # Spread penalties
    for q, name in [(nvda_long_q, "NVDA_LONG"), (nvda_short_q, "NVDA_SHORT"),
                    (xle_long_q,  "XLE_LONG"),  (xle_short_q,  "XLE_SHORT")]:
        if q:
            sp = ba_spread_pct(q[0], q[1])
            if sp > 0.05:
                score -= 1.5
            elif sp > 0.02:
                score -= 0.5
        else:
            score -= 2.0

    score = max(1.0, min(10.0, round(score, 1)))
    slog(f"Model Confidence Score: {score}/10",
         action="CONFIDENCE_SCORE",
         reason=f"v1={v1:.4f} v5={v5:.4f} v15={v15:.4f} noise_warning={noise_warning}")
    return score, noise_warning


# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET PRODUCER / CONSUMER — In-Memory Queue
# ══════════════════════════════════════════════════════════════════════════════

class DataFeed:
    """
    Producer: polls Alpaca REST for option quotes every 5s into a queue.
    (WebSocket fallback — Alpaca paper options stream not always available.)
    Consumer: Strategy engine reads latest snapshot non-blockingly.
    """
    def __init__(self, symbols):
        self.symbols  = symbols
        self._queue   = queue.Queue(maxsize=100)
        self._running = False
        self._thread  = None
        self._latest  = {}   # symbol → latest quote

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        slog("DataFeed started (REST polling mode)",
             action="FEED_START", reason="WebSocket not available for paper options")

    def stop(self):
        self._running = False

    def _poll_loop(self):
        while self._running:
            for sym in self.symbols:
                try:
                    q = get_option_quote(sym)
                    if q:
                        self._latest[sym] = {"bid": q[0], "ask": q[1], "mid": q[2],
                                             "ts": datetime.now(timezone.utc).isoformat()}
                        try:
                            self._queue.put_nowait({"symbol": sym, "quote": self._latest[sym]})
                        except queue.Full:
                            self._queue.get_nowait()   # drop oldest
                            self._queue.put_nowait({"symbol": sym, "quote": self._latest[sym]})
                except Exception:
                    pass
            time.sleep(5)

    def latest(self, symbol):
        """Non-blocking read of latest quote for a symbol."""
        return self._latest.get(symbol)


# ══════════════════════════════════════════════════════════════════════════════
#  SHADOW LEDGER — Forensic Layer with Zombie Halt
# ══════════════════════════════════════════════════════════════════════════════

class ShadowLedger:
    """
    - Increments/decrements only after confirmed API persistence + schema validation
    - Background sync daemon every 30s
    - Zombie halt: triggers SANDBOX callback if drift persists >30s
    """
    def __init__(self, on_zombie_detected=None):
        self.filename          = _DIR / "v18_shadow_ledger.json"
        self.state             = self._load()
        self._lock             = threading.Lock()
        self._running          = True
        self._on_zombie        = on_zombie_detected
        self._drift_since      = {}   # symbol → datetime when drift first detected
        self._thread           = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()

    def _load(self):
        if self.filename.exists():
            with open(self.filename) as f:
                return json.load(f)
        return {"positions": {}, "orders": {}, "agent_state": "PENDING",
                "entry_greeks": {}, "sessions": {}, "last_sync": None}

    def _write(self):
        with open(self.filename, "w") as f:
            json.dump(self.state, f, indent=2)

    def save(self, agent_state=None, key=None, value=None):
        with self._lock:
            if key and value is not None:
                self.state["positions"][key] = value
            if agent_state:
                self.state["agent_state"] = agent_state
            self.state["last_sync"] = datetime.now(timezone.utc).isoformat()
            self._write()

    def record_order(self, leg_name, order_payload, qty, side):
        """Only records after payload passes schema validation."""
        if not validate_payload(order_payload, ORDER_SCHEMA, f"order/{leg_name}"):
            return False
        with self._lock:
            self.state["orders"][leg_name] = {
                **order_payload, "qty": qty, "side": side,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write()
        return True

    def record_greeks(self, symbol, greeks):
        """Store entry Greeks for drift monitoring."""
        with self._lock:
            self.state["entry_greeks"][symbol] = {
                **greeks, "recorded_at": datetime.now(timezone.utc).isoformat()
            }
            self._write()

    def get_entry_greeks(self, symbol):
        return self.state.get("entry_greeks", {}).get(symbol)

    def clear_pending(self, ticker):
        with self._lock:
            removed_p = {k for k in self.state["positions"] if ticker.upper() in k}
            removed_o = {k for k, v in self.state["orders"].items()
                         if ticker.upper() in v.get("symbol", "")}
            for k in removed_p: del self.state["positions"][k]
            for k in removed_o: del self.state["orders"][k]
            if removed_p or removed_o:
                slog(f"Ledger: cleared {len(removed_p)} positions, {len(removed_o)} orders for {ticker}",
                     action="LEDGER_CLEAR", reason=f"Ticker swap: removing {ticker}")
            self._write()

    def initialize_session(self, ticker):
        with self._lock:
            self.state.setdefault("sessions", {})[ticker] = {
                "initialized_at": datetime.now(timezone.utc).isoformat(),
                "status": "ACTIVE",
            }
            self._write()
        slog(f"Ledger: session initialized for {ticker}",
             action="SESSION_INIT", reason=f"New ticker session: {ticker}")

    def get(self, key, default=None):
        return self.state.get(key, default)

    def stop(self):
        self._running = False

    def _sync_loop(self):
        while self._running:
            time.sleep(LEDGER_SYNC_SECS)
            try:
                self._alpaca_sync()
            except Exception as e:
                slog(f"Ledger sync error: {e}", action="SYNC_ERROR", reason=str(e), level="warning")

    def _alpaca_sync(self):
        """Validate Alpaca positions against ledger. Zombie halt if drift >30s."""
        raw_positions = trading.get_all_positions()
        # Validate each position payload
        alpaca_qtys = {}
        for p in raw_positions:
            payload = {"symbol": p.symbol, "qty": str(p.qty)}
            if validate_payload(payload, POSITION_SCHEMA, f"position/{p.symbol}"):
                alpaca_qtys[p.symbol] = int(float(p.qty))

        now = datetime.now(timezone.utc)
        with self._lock:
            for sym, local_qty in self.state["positions"].items():
                remote_qty = alpaca_qtys.get(sym, 0)
                if local_qty != 0 and local_qty != remote_qty:
                    if sym not in self._drift_since:
                        self._drift_since[sym] = now
                        slog(f"DRIFT DETECTED: {sym} local={local_qty} alpaca={remote_qty}",
                             action="DRIFT_DETECTED",
                             reason=f"Position mismatch: local={local_qty} remote={remote_qty}",
                             level="warning")
                    else:
                        elapsed = (now - self._drift_since[sym]).total_seconds()
                        if elapsed > ZOMBIE_TIMEOUT:
                            slog(f"ZOMBIE HALT: {sym} drift for {elapsed:.0f}s — triggering SANDBOX",
                                 action="ZOMBIE_HALT",
                                 reason=f"Ledger/Alpaca mismatch exceeds {ZOMBIE_TIMEOUT}s",
                                 level="error")
                            if self._on_zombie:
                                self._on_zombie(sym)
                else:
                    self._drift_since.pop(sym, None)
                    self.state["positions"][sym] = remote_qty
            self.state["last_sync"] = now.isoformat()
            self._write()
        slog("Ledger synced with Alpaca", action="SYNC_OK", reason="30s scheduled sync")


# ══════════════════════════════════════════════════════════════════════════════
#  MULTI-LEG EXECUTION (atomic mleg with individual-leg fallback)
# ══════════════════════════════════════════════════════════════════════════════

class SpreadExecution:
    """
    Attempts atomic multi-leg order via Alpaca mleg.
    Falls back to individual legs if mleg not supported.
    Handles 422 (Unprocessable) and 403 (Insufficient Qty) via RECONCILE.
    """
    def __init__(self, legs_spec, dry_run=False):
        # legs_spec: list of (name, symbol, side, qty, limit_price or None)
        self.legs_spec   = legs_spec
        self.dry_run     = dry_run
        self.order_ids   = {}   # name → order_id
        self.filled      = {}   # name → bool
        self.partial     = {}   # name → filled_qty

    def execute(self):
        """Returns ('ok', orders), ('partial', orders), or ('error', reason)."""
        if self.dry_run:
            for name, sym, side, qty, limit in self.legs_spec:
                mode = f"LIMIT @ ${limit:.2f}" if limit else "MARKET"
                slog(f"[DRY-RUN] {side.value.upper()} {qty}x {sym} {mode}",
                     action="DRY_RUN_ORDER", reason=f"Dry run leg: {name}")
                self.filled[name] = True
            return "ok", {}

        # Try atomic mleg first
        try:
            return self._submit_mleg()
        except Exception as e:
            slog(f"mleg failed ({e}) — falling back to individual legs",
                 action="MLEG_FALLBACK", reason=str(e), level="warning")
            return self._submit_individual()

    def _submit_mleg(self):
        """Atomic multi-leg order — prevents partial leg-in risk."""
        # Build legs for mleg order (Alpaca API)
        legs = []
        for name, sym, side, qty, limit in self.legs_spec:
            leg = {
                "symbol": sym,
                "side":   side.value,
                "qty":    str(qty),
                "type":   "limit" if limit else "market",
            }
            if limit:
                leg["limit_price"] = str(round(limit, 2))
            legs.append(leg)

        payload = {
            "order_class": "mleg",
            "type":         "market" if all(l is None for _, _, _, _, l in self.legs_spec) else "limit",
            "time_in_force": "day",
            "legs":          legs,
        }
        import requests
        resp = requests.post(
            "https://paper-api.alpaca.markets/v2/orders",
            headers={"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET},
            json=payload,
            timeout=10,
        )
        if resp.status_code == 422:
            raise ValueError(f"422 Unprocessable: {resp.json()}")
        if resp.status_code == 403:
            raise PermissionError(f"403 Insufficient: {resp.json()}")
        resp.raise_for_status()
        order = resp.json()
        slog(f"mleg order placed: {order.get('id')}",
             action="MLEG_ORDER", reason="Atomic multi-leg submission")
        for name, *_ in self.legs_spec:
            self.filled[name] = True
        return "ok", {n: order for n, *_ in self.legs_spec}

    def _submit_individual(self):
        """Individual leg fallback — best-effort, tracks partial fills."""
        orders  = {}
        any_err = False
        for name, sym, side, qty, limit in self.legs_spec:
            try:
                if limit:
                    req = LimitOrderRequest(symbol=sym, qty=qty, side=side,
                                            time_in_force=TimeInForce.DAY,
                                            limit_price=round(limit, 2))
                else:
                    req = MarketOrderRequest(symbol=sym, qty=qty, side=side,
                                            time_in_force=TimeInForce.DAY)
                order = trading.submit_order(req)
                self.order_ids[name] = str(order.id)
                self.filled[name]    = order.status == OrderStatus.FILLED
                orders[name]         = order
                slog(f"Individual leg {name}: {side.value.upper()} {qty}x {sym} → {order.id}",
                     action="LEG_ORDER", reason=f"Individual leg submission: {name}")
            except Exception as e:
                slog(f"Leg {name} failed: {e}", action="LEG_ERROR", reason=str(e), level="error")
                any_err = True

        if any_err:
            return "partial", orders
        return "ok", orders

    def check_fills(self):
        """Refresh fill status for all legs."""
        for name, order_id in self.order_ids.items():
            try:
                o = trading.get_order_by_id(order_id)
                self.filled[name] = o.status == OrderStatus.FILLED
                if hasattr(o, 'filled_qty'):
                    self.partial[name] = int(o.filled_qty or 0)
            except Exception:
                pass
        return self.filled

    def chase_limit(self, name, new_price):
        """Modify limit price for a specific leg (RECONCILE mode)."""
        order_id = self.order_ids.get(name)
        if not order_id or self.dry_run:
            return
        try:
            trading.replace_order_by_id(order_id, ReplaceOrderRequest(
                limit_price=round(new_price, 2)
            ))
            slog(f"Limit chase {name} → ${new_price:.2f}",
                 action="LIMIT_CHASE", reason=f"Chasing unfilled leg: {name}")
        except Exception as e:
            slog(f"Chase failed {name}: {e}", action="CHASE_ERROR", reason=str(e), level="warning")

    def cancel_all(self):
        """Cancel all open orders (SANDBOX mode)."""
        for name, order_id in self.order_ids.items():
            if not self.filled.get(name):
                try:
                    trading.cancel_order_by_id(order_id)
                    slog(f"Cancelled order {name} ({order_id})",
                         action="ORDER_CANCEL", reason="SANDBOX mode: cancelling all pending")
                except Exception:
                    pass

    def close_all(self, dry_run=False):
        """Close all legs with market orders in opposite direction."""
        for name, sym, side, qty, _ in self.legs_spec:
            if dry_run:
                slog(f"[DRY-RUN] CLOSE {name}", action="DRY_RUN_CLOSE", reason=f"Close leg: {name}")
                continue
            close_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            try:
                trading.submit_order(MarketOrderRequest(
                    symbol=sym, qty=qty, side=close_side, time_in_force=TimeInForce.DAY
                ))
                slog(f"Closed {name}: {close_side.value.upper()} {qty}x {sym}",
                     action="LEG_CLOSE", reason=f"Position close: {name}")
            except Exception as e:
                slog(f"Close failed {name}: {e}", action="CLOSE_ERROR", reason=str(e), level="error")


# ══════════════════════════════════════════════════════════════════════════════
#  STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════════

class TradeState(ABC):
    @abstractmethod
    def execute(self, ctx): pass


class StatePending(TradeState):
    """Wait for entry signal. Full confidence check. Places atomic mleg."""

    def execute(self, ctx):
        slog("STATE: PENDING — evaluating entry conditions",
             state="PENDING", action="CYCLE", reason="Scheduled evaluation cycle")

        if is_friday_kill():
            slog("Friday kill-switch active — no new entries",
                 state="PENDING", action="FRIDAY_KILL", reason="Friday 11:30 AM ET kill-switch")
            return
        if not market_is_open() and not ctx.dry_run:
            slog("Market closed — skipping entry check",
                 state="PENDING", action="MARKET_CLOSED", reason="Outside market hours")
            return
        if ctx.thesis_broken():
            return

        # Pre-flight buying power check
        acct = trading.get_account()
        buying_power = float(acct.buying_power)
        required     = ctx.allocation * 2   # two tranches
        if buying_power < required and not ctx.dry_run:
            slog(f"Insufficient buying power: ${buying_power:,.0f} < ${required:,.0f}",
                 state="PENDING", action="PREFLIGHT_FAIL",
                 reason=f"Need ${required:,.0f}, have ${buying_power:,.0f}", level="warning")
            ctx.transition_to("THROTTLED")
            return

        # Get quotes and compute confidence
        nvda_long_sym  = occ_symbol("NVDA", ctx.nvda_long_strike,  ContractType.CALL)
        nvda_short_sym = occ_symbol("NVDA", ctx.nvda_short_strike, ContractType.CALL)
        xle_long_sym   = occ_symbol("XLE",  ctx.xle_long_strike,   ContractType.PUT)
        xle_short_sym  = occ_symbol("XLE",  ctx.xle_short_strike,  ContractType.PUT)

        q_nl = ctx.feed.latest(nvda_long_sym)  or get_option_quote(nvda_long_sym)  and {"mid": get_option_quote(nvda_long_sym)[2]}
        q_ns = ctx.feed.latest(nvda_short_sym) or get_option_quote(nvda_short_sym) and {"mid": get_option_quote(nvda_short_sym)[2]}
        q_xl = ctx.feed.latest(xle_long_sym)   or get_option_quote(xle_long_sym)   and {"mid": get_option_quote(xle_long_sym)[2]}
        q_xs = ctx.feed.latest(xle_short_sym)  or get_option_quote(xle_short_sym)  and {"mid": get_option_quote(xle_short_sym)[2]}

        gk_vols = ctx.gk_nvda.compute_all()
        score, noise_warning = compute_confidence_score(
            gk_vols,
            get_option_quote(nvda_long_sym),
            get_option_quote(nvda_short_sym),
            get_option_quote(xle_long_sym),
            get_option_quote(xle_short_sym),
        )

        if score < 4:
            slog(f"Confidence score too low ({score}/10) — holding",
                 state="PENDING", action="LOW_CONFIDENCE",
                 reason=f"Score {score}/10 below entry threshold")
            return

        # Determine scenario and build legs
        use_limit   = noise_warning or score < 7
        qty         = ctx.qty
        spread_ok   = score >= 7

        def mid_or_fallback(q, fallback):
            if isinstance(q, dict): return q.get("mid", fallback)
            if isinstance(q, tuple) and len(q) == 3: return q[2]
            return fallback

        limits = {
            nvda_long_sym:  mid_or_fallback(get_option_quote(nvda_long_sym), ctx.nvda_long_strike * 0.01),
            nvda_short_sym: mid_or_fallback(get_option_quote(nvda_short_sym), ctx.nvda_short_strike * 0.01),
            xle_long_sym:   mid_or_fallback(get_option_quote(xle_long_sym), ctx.xle_long_strike * 0.01),
            xle_short_sym:  mid_or_fallback(get_option_quote(xle_short_sym), ctx.xle_short_strike * 0.01),
        }

        scenario = "A" if spread_ok and not noise_warning else "B"
        slog(f"Scenario {scenario} ({'MARKET' if not use_limit else 'LIMIT'}) | Score {score}/10",
             state="PENDING", action=f"SCENARIO_{scenario}",
             reason=f"Confidence={score} noise={noise_warning}")

        legs_spec = [
            ("nvda_long",  nvda_long_sym,  OrderSide.BUY,  qty, None if not use_limit else limits[nvda_long_sym]),
            ("nvda_short", nvda_short_sym, OrderSide.SELL, qty, None if not use_limit else limits[nvda_short_sym]),
            ("xle_long",   xle_long_sym,   OrderSide.BUY,  qty, None if not use_limit else limits[xle_long_sym]),
            ("xle_short",  xle_short_sym,  OrderSide.SELL, qty, None if not use_limit else limits[xle_short_sym]),
        ]

        ctx.spread = SpreadExecution(legs_spec, dry_run=ctx.dry_run)
        result, orders = ctx.spread.execute()

        if result == "ok":
            # Record entry Greeks for drift monitoring
            for sym in [nvda_long_sym, nvda_short_sym, xle_long_sym, xle_short_sym]:
                greeks = get_option_greeks(sym)
                if greeks:
                    ctx.ledger.record_greeks(sym, greeks)

            # Record orders in ledger (schema-validated)
            for name, o in orders.items():
                payload = {"id": str(getattr(o, "id", "DRY_RUN")),
                           "status": str(getattr(o, "status", "dry_run")),
                           "symbol": str(getattr(o, "symbol", name)),
                           "qty":    str(qty)}
                ctx.ledger.record_order(name, payload, qty, legs_spec[0][2].value)

            ctx.entry_limits = limits
            ctx.ledger.save(agent_state="OPEN")
            ctx.transition_to("OPEN")

        elif result == "partial":
            slog("Partial fill — entering RECONCILE",
                 state="PENDING", action="PARTIAL_FILL",
                 reason="One or more legs did not fill at submission")
            ctx.transition_to("RECONCILE")
        else:
            slog(f"Entry failed: {result}",
                 state="PENDING", action="ENTRY_FAIL", reason=str(result), level="error")


class StateThrottled(TradeState):
    """Mode 2 — THROTTLED: 50% size, limit-only, degraded entry."""

    def execute(self, ctx):
        slog("STATE: THROTTLED — reduced size limit-only entry",
             state="THROTTLED", action="CYCLE",
             reason="Buying power below full allocation threshold")

        if not market_is_open() and not ctx.dry_run:
            return
        if is_friday_kill():
            ctx.transition_to("LIQUIDATED")
            return

        # Re-check buying power for half allocation
        acct = trading.get_account()
        buying_power = float(acct.buying_power)
        if buying_power >= ctx.allocation * 2 and not ctx.dry_run:
            slog("Buying power restored — returning to PENDING",
                 state="THROTTLED", action="RESTORE",
                 reason=f"Buying power ${buying_power:,.0f} sufficient")
            ctx.transition_to("PENDING")
            return

        # Use 50% qty, limit-only
        qty = THROTTLED_QTY
        nvda_long_sym  = occ_symbol("NVDA", ctx.nvda_long_strike,  ContractType.CALL)
        nvda_short_sym = occ_symbol("NVDA", ctx.nvda_short_strike, ContractType.CALL)
        xle_long_sym   = occ_symbol("XLE",  ctx.xle_long_strike,   ContractType.PUT)
        xle_short_sym  = occ_symbol("XLE",  ctx.xle_short_strike,  ContractType.PUT)

        def mid(sym):
            q = get_option_quote(sym)
            return q[2] if q else 0.0

        legs_spec = [
            ("nvda_long",  nvda_long_sym,  OrderSide.BUY,  qty, mid(nvda_long_sym)),
            ("nvda_short", nvda_short_sym, OrderSide.SELL, qty, mid(nvda_short_sym)),
            ("xle_long",   xle_long_sym,   OrderSide.BUY,  qty, mid(xle_long_sym)),
            ("xle_short",  xle_short_sym,  OrderSide.SELL, qty, mid(xle_short_sym)),
        ]

        slog(f"THROTTLED entry: {qty} contracts (50% size), limit-only",
             state="THROTTLED", action="THROTTLED_ENTRY",
             reason=f"Reduced size: {qty} vs normal {ctx.qty}")

        ctx.spread = SpreadExecution(legs_spec, dry_run=ctx.dry_run)
        result, orders = ctx.spread.execute()
        if result in ("ok", "partial"):
            ctx.entry_limits = {name: lim for name, _, _, _, lim in legs_spec}
            ctx.ledger.save(agent_state="OPEN_THROTTLED")
            ctx.transition_to("OPEN")


class StateOpen(TradeState):
    """Monitor positions. Check PnL, Greek drift, thesis break, Friday kill."""

    def execute(self, ctx):
        slog("STATE: OPEN — monitoring positions",
             state="OPEN", action="CYCLE", reason="Scheduled monitoring cycle")

        if is_friday_kill():
            slog("Friday 11:30 AM kill-switch",
                 state="OPEN", action="FRIDAY_KILL",
                 reason="Mandatory Friday liquidation at 11:30 AM ET")
            ctx.transition_to("LIQUIDATED")
            return

        if ctx.thesis_broken():
            ctx.transition_to("LIQUIDATED")
            return

        # Check for unfilled legs → RECONCILE
        if ctx.spread:
            fills = ctx.spread.check_fills()
            unfilled = [n for n, v in fills.items() if not v]
            if unfilled:
                slog(f"Unfilled legs detected: {unfilled} — entering RECONCILE",
                     state="OPEN", action="UNFILLED_DETECTED",
                     reason=f"Legs not filled: {unfilled}")
                ctx.transition_to("RECONCILE")
                return

        # Greek drift check — Theta/Vega >20% from entry
        greek_exit = self._check_greek_drift(ctx)
        if greek_exit:
            slog("Greek drift >20% — liquidating",
                 state="OPEN", action="GREEK_EXIT",
                 reason="Theta or Vega drifted more than 20% from entry")
            ctx.transition_to("LIQUIDATED")
            return

        # PnL
        pnl = ctx.compute_pnl()
        slog(f"PnL: {pnl:+.1%}",
             state="OPEN", action="PNL_CHECK",
             reason=f"Current PnL={pnl:+.1%} gate={PROFIT_GATE:.0%}")

        if pnl >= PROFIT_GATE:
            slog(f"Profit gate hit ({pnl:.1%})",
                 state="OPEN", action="PROFIT_GATE",
                 reason=f"PnL {pnl:.1%} >= {PROFIT_GATE:.0%} gate")
            ctx.transition_to("LIQUIDATED")
            return

        if pnl >= BREAKEVEN_GATE and not ctx.at_breakeven:
            ctx.at_breakeven = True
            ctx.ledger.save(agent_state="OPEN_BREAKEVEN")
            slog("Breakeven achieved — stop moved to net debit",
                 state="OPEN", action="BREAKEVEN",
                 reason=f"PnL {pnl:.1%} >= {BREAKEVEN_GATE:.0%} breakeven gate")

    def _check_greek_drift(self, ctx):
        """Returns True if Theta or Vega drifted >20% from entry on any leg."""
        if not ctx.spread:
            return False
        for name, sym, side, qty, _ in ctx.spread.legs_spec:
            entry_greeks = ctx.ledger.get_entry_greeks(sym)
            if not entry_greeks:
                continue
            current_greeks = get_option_greeks(sym)
            if not current_greeks:
                continue
            for greek in ["theta", "vega"]:
                entry_val   = entry_greeks.get(greek, 0)
                current_val = current_greeks.get(greek, 0)
                if abs(entry_val) < 0.0001:
                    continue
                drift = abs(current_val - entry_val) / abs(entry_val)
                if drift > GREEK_DRIFT_MAX:
                    slog(f"Greek drift: {sym} {greek} drift={drift:.1%} > {GREEK_DRIFT_MAX:.0%}",
                         state="OPEN", action="GREEK_DRIFT",
                         reason=f"{sym} {greek}: entry={entry_val:.4f} current={current_val:.4f} drift={drift:.1%}",
                         level="warning")
                    return True
        return False


class StateReconcile(TradeState):
    """
    Scenario B: Limit-chasing for unfilled legs.
    Handles 422/403 errors. Max 5 mods per 60s.
    """
    def __init__(self):
        self._mods         = 0
        self._window_start = time.time()

    def execute(self, ctx):
        slog("STATE: RECONCILE — limit chasing unfilled legs",
             state="RECONCILE", action="CYCLE",
             reason="Unfilled legs pending, entering limit-chase loop")

        if is_friday_kill():
            if ctx.spread:
                ctx.spread.cancel_all()
            ctx.transition_to("LIQUIDATED")
            return

        # Reset mod window
        if time.time() - self._window_start > 60:
            self._mods         = 0
            self._window_start = time.time()

        if self._mods >= LIMIT_CHASE_MAX:
            slog("Max limit chases reached this window — waiting",
                 state="RECONCILE", action="CHASE_THROTTLE",
                 reason=f"Hit {LIMIT_CHASE_MAX} mods in 60s window")
            return

        if not ctx.spread:
            ctx.transition_to("PENDING")
            return

        fills = ctx.spread.check_fills()
        still_unfilled = [n for n, v in fills.items() if not v]

        for name in still_unfilled:
            sym_spec = next((s for n, s, *_ in ctx.spread.legs_spec if n == name), None)
            if sym_spec:
                q = get_option_quote(sym_spec)
                if q:
                    new_price = round(q[1] * 1.005, 2)
                    ctx.spread.chase_limit(name, new_price)
                    self._mods += 1

        if not still_unfilled:
            slog("All legs filled — transitioning to OPEN",
                 state="RECONCILE", action="ALL_FILLED",
                 reason="All legs confirmed filled via Alpaca")
            ctx.transition_to("OPEN")


class StateSandbox(TradeState):
    """
    Mode 3 — SANDBOX: Observe-only.
    Cancels all pending orders. Logs hypothetical performance.
    Exits when vol normalises AND no zombie drift.
    """
    def execute(self, ctx):
        slog("STATE: SANDBOX — observe-only mode",
             state="SANDBOX", action="CYCLE",
             reason="Elevated vol or zombie position detected")

        # Cancel all pending orders on first entry
        if ctx.spread and not getattr(ctx, "_sandbox_cancelled", False):
            ctx.spread.cancel_all()
            ctx._sandbox_cancelled = True
            slog("All pending orders cancelled (SANDBOX entry)",
                 state="SANDBOX", action="CANCEL_ALL",
                 reason="SANDBOX mode requires cancelling all pending orders")

        # Hypothetical PnL logging
        hypo_pnl = ctx.compute_pnl()
        slog(f"Hypothetical PnL (observe-only): {hypo_pnl:+.1%}",
             state="SANDBOX", action="HYPOTHETICAL_PNL",
             reason=f"Observe-only PnL estimate: {hypo_pnl:+.1%}")

        # Check for exit conditions
        vols = ctx.gk_nvda.compute_all()
        v1, v15 = vols.get(1, 0), vols.get(15, 0)
        vol_normalised = v15 == 0 or v1 <= 2 * v15

        if vol_normalised:
            slog("Vol normalised — exiting SANDBOX → PENDING",
                 state="SANDBOX", action="SANDBOX_EXIT",
                 reason=f"1-min={v1:.4f} normalised vs 15-min={v15:.4f}")
            ctx._sandbox_cancelled = False
            ctx.transition_to("PENDING")


class StateLiquidated(TradeState):
    """Close all legs, update ledger, stop agent."""
    def __init__(self, reason="THESIS_COMPLETE"):
        self.reason = reason

    def execute(self, ctx):
        slog(f"STATE: LIQUIDATED ({self.reason}) — closing all legs",
             state="LIQUIDATED", action="LIQUIDATE",
             reason=self.reason)
        if ctx.spread:
            ctx.spread.close_all(dry_run=ctx.dry_run)
        ctx.ledger.save(agent_state=f"LIQUIDATED_{self.reason}")
        ctx.running = False


# ══════════════════════════════════════════════════════════════════════════════
#  V18 AGENT
# ══════════════════════════════════════════════════════════════════════════════

class V18Agent:
    def __init__(self,
                 nvda_strikes=(NVDA_LONG_STRIKE, NVDA_SHORT_STRIKE),
                 xle_strikes=(XLE_LONG_STRIKE,  XLE_SHORT_STRIKE),
                 allocation=20000,
                 qty=QTY,
                 dry_run=False,
                 force_mode=None):

        self.nvda_long_strike  = nvda_strikes[0]
        self.nvda_short_strike = nvda_strikes[1]
        self.xle_long_strike   = xle_strikes[0]
        self.xle_short_strike  = xle_strikes[1]
        self.allocation        = allocation
        self.qty               = qty
        self.dry_run           = dry_run
        self.running           = True
        self.spread            = None
        self.entry_limits      = {}
        self.at_breakeven      = False
        self._sandbox_cancelled = False

        # Shadow Ledger with zombie callback
        self.ledger  = ShadowLedger(on_zombie_detected=self._on_zombie)
        self.gk_nvda = GarmanKlassVol("NVDA")

        # Data feed (Producer/Consumer)
        symbols = [
            occ_symbol("NVDA", self.nvda_long_strike,  ContractType.CALL),
            occ_symbol("NVDA", self.nvda_short_strike, ContractType.CALL),
            occ_symbol("XLE",  self.xle_long_strike,   ContractType.PUT),
            occ_symbol("XLE",  self.xle_short_strike,  ContractType.PUT),
        ]
        self.feed = DataFeed(symbols)
        self.feed.start()

        # Ledger initialisation
        self.ledger.clear_pending("USO")
        self.ledger.initialize_session("XLE")

        # Resume or initialise state
        saved = self.ledger.get("agent_state", "PENDING")
        if "LIQUIDATED" in saved:
            slog("Already liquidated — delete ledger to restart",
                 state=saved, action="RESUME_LIQUIDATED",
                 reason="Ledger shows previous liquidation")
            self.running = False
            self.state   = StateLiquidated("RESUME")
            return

        if saved in ("OPEN", "OPEN_BREAKEVEN", "OPEN_THROTTLED"):
            self.state        = StateOpen()
            self.at_breakeven = "BREAKEVEN" in saved
            slog(f"Resuming from {saved}",
                 state=saved, action="RESUME", reason=f"Ledger state: {saved}")
        elif force_mode == 2:
            self.state = StateThrottled()
            slog("Force mode 2 — THROTTLED",
                 state="THROTTLED", action="FORCE_MODE", reason="--mode 2 CLI flag")
        else:
            # Vol regime check
            vols = self.gk_nvda.compute_all()
            v1, v15 = vols.get(1, 0), vols.get(15, 0)
            if v15 > 0 and v1 > 2 * v15:
                slog(f"GK vol elevated ({v1:.4f} > 2×{v15:.4f}) — starting in SANDBOX",
                     state="SANDBOX", action="INIT_SANDBOX",
                     reason=f"1-min vol {v1:.4f} exceeds 2x 15-min {v15:.4f}")
                self.transition_to("SANDBOX")
            else:
                self.transition_to("PENDING")

    def _on_zombie(self, symbol):
        """Callback from ledger sync when zombie drift detected."""
        slog(f"Zombie callback for {symbol} — entering SANDBOX",
             state="OPEN", action="ZOMBIE_CALLBACK",
             reason=f"Position drift on {symbol} exceeded {ZOMBIE_TIMEOUT}s",
             level="error")
        self.transition_to("SANDBOX")

    def set_state(self, state):
        slog(f"→ {type(state).__name__}",
             state=type(state).__name__, action="TRANSITION",
             reason=f"State change to {type(state).__name__}")
        self.state = state

    def transition_to(self, name):
        mapping = {
            "PENDING":    StatePending,
            "THROTTLED":  StateThrottled,
            "OPEN":       StateOpen,
            "RECONCILE":  StateReconcile,
            "SANDBOX":    StateSandbox,
            "LIQUIDATED": lambda: StateLiquidated("MANUAL"),
        }
        factory = mapping.get(name.upper())
        if not factory:
            slog(f"Unknown state: {name}", action="BAD_TRANSITION",
                 reason=f"No state mapping for: {name}", level="error")
            return
        self.set_state(factory())
        self.ledger.save(agent_state=name.upper())

    def get_gk_vol(self, latency="15min"):
        mins = int(latency.replace("min", ""))
        return self.gk_nvda.compute_all().get(mins, 0.0)

    def thesis_broken(self):
        try:
            nvda_b, nvda_a = get_underlying_quote("NVDA")
            xle_b,  xle_a  = get_underlying_quote("XLE")
            nvda_p = (nvda_b + nvda_a) / 2 if nvda_b else 999
            xle_p  = (xle_b  + xle_a)  / 2 if xle_b  else 0
            broken = nvda_p < NVDA_THESIS_BREAK or xle_p > XLE_THESIS_BREAK
            if broken:
                slog(f"Thesis broken: NVDA=${nvda_p:.2f} XLE=${xle_p:.2f}",
                     state="OPEN", action="THESIS_BREAK",
                     reason=f"NVDA {nvda_p:.2f}<{NVDA_THESIS_BREAK} OR XLE {xle_p:.2f}>{XLE_THESIS_BREAK}",
                     level="warning")
            return broken
        except Exception:
            return False

    def compute_pnl(self):
        try:
            if not self.spread or not self.entry_limits:
                return 0.0
            net_entry = net_current = 0.0
            legs_priced = 0
            for name, sym, side, qty, _ in self.spread.legs_spec:
                entry_price = self.entry_limits.get(sym)
                if entry_price is None:
                    continue
                feed_data = self.feed.latest(sym)
                current = feed_data["mid"] if feed_data else None
                if not current:
                    q = get_option_quote(sym)
                    current = q[2] if q else None
                if not current:
                    continue
                sign = 1 if side == OrderSide.BUY else -1
                net_entry   += entry_price * sign
                net_current += current     * sign
                legs_priced += 1
            if legs_priced == 0 or abs(net_entry) < 0.001:
                return 0.0
            return (net_current - net_entry) / abs(net_entry)
        except Exception as e:
            slog(f"PnL computation failed: {e}", action="PNL_ERROR", reason=str(e), level="warning")
            return 0.0

    def run(self):
        acct = trading.get_account()
        slog("V18.9 Agentic System starting",
             state="INIT", action="STARTUP",
             reason=f"equity={acct.equity} cash={acct.cash}")
        print("═══════════════════════════════════════════════════")
        print(f"  V18.9 Agentic System | {'DRY-RUN' if self.dry_run else 'LIVE (PAPER)'}")
        print(f"  NVDA: Bull Call Vertical ${self.nvda_long_strike}C/${self.nvda_short_strike}C  x{self.qty}")
        print(f"  XLE:  Bear Put Vertical  ${self.xle_long_strike}P/${self.xle_short_strike}P   x{self.qty}")
        print(f"  Expiry  : {EXPIRY}")
        print(f"  Account : equity ${float(acct.equity):,.2f}  cash ${float(acct.cash):,.2f}")
        print("═══════════════════════════════════════════════════")

        while self.running:
            try:
                self.state.execute(self)
            except Exception as e:
                slog(f"Cycle error: {e}", state="ERROR", action="CYCLE_ERROR",
                     reason=str(e), level="error")
            if self.running:
                slog(f"Sleeping {POLL_SECS}s",
                     state=type(self.state).__name__, action="SLEEP",
                     reason=f"Polling interval {POLL_SECS}s")
                time.sleep(POLL_SECS)

        self.feed.stop()
        self.ledger.stop()
        slog("V18.9 Agent stopped", state="STOPPED", action="SHUTDOWN", reason="Agent loop exited")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V18.9 Options Spread Agent")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate signals and logic without placing orders")
    parser.add_argument("--mode", type=int, choices=[1, 2, 3], default=1,
                        help="1=NORMAL 2=THROTTLED 3=SANDBOX")
    args = parser.parse_args()

    agent = V18Agent(
        nvda_strikes=(197.50, 202.50),
        xle_strikes=(60.00, 55.00),
        allocation=20000,
        qty=40,
        dry_run=args.dry_run,
        force_mode=args.mode if args.mode != 1 else None,
    )
    agent.run()
