"""
Executes trades on the Alpaca paper account.

Core + satellite strategy:
  - BASE_HOLDING (SPY) absorbs idle cash so it earns market returns
  - When a politician BUY arrives  → sell enough SPY to fund it
  - When a politician SELL arrives → liquidate stock, rebuy SPY with proceeds
  - TRADE_SIZE_PCT is now 15% of total equity (not just cash) per signal
"""

import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
    TRADE_SIZE_PCT, CASH_RESERVE_PCT,
    BASE_HOLDING, BASE_HOLDING_PCT,
    POSITION_CAP_PCT,
)

log = logging.getLogger(__name__)

_client: TradingClient | None = None


def get_client() -> TradingClient:
    global _client
    if _client is None:
        _client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
    return _client


def get_account():
    return get_client().get_account()


def get_equity() -> float:
    return float(get_account().equity)


def get_available_cash() -> float:
    return float(get_account().cash)


def get_position_qty(symbol: str) -> float:
    try:
        pos = get_client().get_open_position(symbol)
        return float(pos.qty)
    except Exception:
        return 0.0


def get_position_value(symbol: str) -> float:
    try:
        pos = get_client().get_open_position(symbol)
        return float(pos.market_value)
    except Exception:
        return 0.0


# ── SPY base-holding management ───────────────────────────────────────────────

def rebalance_to_base():
    """
    After startup or after a sell, top up SPY back toward BASE_HOLDING_PCT of equity.
    Only buys — never forces a sell of SPY here (that happens in fund_trade).
    """
    equity      = get_equity()
    target_spy  = round(equity * BASE_HOLDING_PCT, 2)
    current_spy = get_position_value(BASE_HOLDING)
    gap         = target_spy - current_spy
    cash        = get_available_cash()
    reserve     = equity * CASH_RESERVE_PCT

    affordable  = max(0.0, cash - reserve)
    buy_amount  = round(min(gap, affordable), 2)

    if buy_amount < 10.0:
        log.info("Base rebalance: SPY already at target (current $%.0f, target $%.0f)",
                 current_spy, target_spy)
        return

    log.info("Base rebalance: buying $%.2f SPY (current $%.0f -> target $%.0f)",
             buy_amount, current_spy, target_spy)
    _market_buy_notional(BASE_HOLDING, buy_amount)


def fund_trade(notional: float) -> float:
    """
    Ensure `notional` dollars of cash are available by trimming SPY if needed.
    Returns the actual cash available after trimming (may be less if SPY is thin).
    """
    cash    = get_available_cash()
    equity  = get_equity()
    reserve = equity * CASH_RESERVE_PCT
    usable  = max(0.0, cash - reserve)

    shortfall = notional - usable
    if shortfall <= 0:
        return notional  # already have enough free cash

    spy_value = get_position_value(BASE_HOLDING)
    trim      = round(min(shortfall, spy_value * 0.999), 2)  # never sell more than we hold
    if trim >= 1.0:
        log.info("Trimming $%.2f SPY to fund trade", trim)
        _market_sell_notional(BASE_HOLDING, trim)

    return min(notional, usable + trim)


# ── Low-level order helpers ───────────────────────────────────────────────────

def _market_buy_notional(symbol: str, notional: float) -> dict | None:
    if notional < 1.0:
        return None
    req = MarketOrderRequest(
        symbol=symbol,
        notional=round(notional, 2),
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )
    try:
        order = get_client().submit_order(req)
        log.info("BUY  %s $%.2f -> order %s", symbol, notional, order.id)
        return {"id": str(order.id), "symbol": symbol, "side": "buy", "notional": notional}
    except Exception as exc:
        log.error("BUY failed for %s: %s", symbol, exc)
        return None


def _market_sell_notional(symbol: str, notional: float) -> dict | None:
    if notional < 1.0:
        return None
    req = MarketOrderRequest(
        symbol=symbol,
        notional=round(notional, 2),
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    try:
        order = get_client().submit_order(req)
        log.info("SELL %s $%.2f -> order %s", symbol, notional, order.id)
        return {"id": str(order.id), "symbol": symbol, "side": "sell", "notional": notional}
    except Exception as exc:
        log.error("SELL failed for %s: %s", symbol, exc)
        return None


def _market_sell_full(symbol: str) -> dict | None:
    qty = get_position_qty(symbol)
    if qty <= 0:
        log.info("No position in %s to sell", symbol)
        return None
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    try:
        order = get_client().submit_order(req)
        log.info("SELL %s %.4f shares -> order %s", symbol, qty, order.id)
        return {"id": str(order.id), "symbol": symbol, "side": "sell", "qty": qty}
    except Exception as exc:
        log.error("SELL failed for %s: %s", symbol, exc)
        return None


# ── Public interface ──────────────────────────────────────────────────────────

def place_buy(symbol: str, notional: float) -> dict | None:
    funded = fund_trade(notional)
    if funded < 1.0:
        log.warning("Could not fund BUY %s (needed $%.2f)", symbol, notional)
        return None
    return _market_buy_notional(symbol, funded)


def place_sell(symbol: str) -> dict | None:
    result = _market_sell_full(symbol)
    if result:
        rebalance_to_base()
    return result


def place_partial_sell(symbol: str, fraction: float) -> dict | None:
    """
    Sell `fraction` (0.0–1.0) of the current position by notional value.
    Treats fraction >= 0.95 as a full exit to avoid tiny residual lots.
    """
    fraction = max(0.0, min(1.0, fraction))
    if fraction >= 0.95:
        return place_sell(symbol)

    pos_value = get_position_value(symbol)
    notional  = round(pos_value * fraction, 2)
    if notional < 1.0:
        log.info("Partial sell %s: notional $%.2f too small — skipping", symbol, notional)
        return None

    log.info("Partial sell %s: %.0f%% of position ($%.2f)", symbol, fraction * 100, notional)
    result = _market_sell_notional(symbol, notional)
    if result:
        rebalance_to_base()
    return result


def execute_trade(trade: dict, size_mult: float = 1.0) -> dict | None:
    symbol     = trade["ticker"]
    action     = trade["action"]
    asset_type = trade.get("asset_type", "Stock").lower()

    if "fund" in asset_type:
        log.info("Skipping fund asset: %s", symbol)
        return None

    if symbol == BASE_HOLDING:
        log.info("Skipping %s — that's our base holding, not a politician signal", symbol)
        return None

    if action == "buy":
        equity   = get_equity()
        notional = round(equity * TRADE_SIZE_PCT * size_mult, 2)

        # 20% single-position cap
        cap         = round(equity * POSITION_CAP_PCT, 2)
        current_val = get_position_value(symbol)
        if current_val >= cap:
            log.info(
                "Position cap: %s already at $%.0f (cap $%.0f) — skipping",
                symbol, current_val, cap,
            )
            return None
        notional = round(min(notional, cap - current_val), 2)

        log.info(
            "Copying BUY %s — base %.0f%% x %.2fx mult = $%.2f (cap headroom $%.0f)",
            symbol, TRADE_SIZE_PCT * 100, size_mult, notional, cap - current_val,
        )
        return place_buy(symbol, notional)

    if action == "sell":
        log.info("Copying SELL %s — liquidating, recycling to SPY", symbol)
        return place_sell(symbol)

    log.info("Unrecognised action '%s' for %s, skipping", action, symbol)
    return None
