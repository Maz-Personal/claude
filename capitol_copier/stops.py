"""
Stop-loss utilities:

  check_trailing_stops() — sell if price drops 10% from peak
  check_time_stops()     — exit flat/losing positions held > 90 days with no fresh signal

Both receive the full state dict, mutate it, and return it.
"""

import logging
from datetime import datetime, timezone, timedelta
from config import TRAILING_STOP_PCT, BASE_HOLDING
from trader import get_client, place_sell

log = logging.getLogger(__name__)


def check_trailing_stops(state: dict) -> dict:
    """
    Inspect every open position, update peak prices, and sell anything that
    has dropped more than TRAILING_STOP_PCT from its peak.

    Modifies state["peaks"] in-place and returns the updated state.
    """
    client = get_client()
    peaks: dict[str, float] = state.setdefault("peaks", {})

    try:
        positions = client.get_all_positions()
    except Exception as exc:
        log.error("Could not fetch positions for trailing-stop check: %s", exc)
        return state

    for pos in positions:
        symbol = pos.symbol
        if symbol == BASE_HOLDING:
            continue  # never stop-out the SPY base holding

        current_price = float(pos.current_price)
        peak = peaks.get(symbol, current_price)

        if current_price > peak:
            peaks[symbol] = current_price
            log.info("Trailing stop: %s new peak $%.2f", symbol, current_price)
        elif current_price < peak * (1.0 - TRAILING_STOP_PCT):
            drop_pct = (peak - current_price) / peak * 100
            log.info(
                "TRAILING STOP: %s dropped %.1f%% from peak $%.2f -> $%.2f — selling",
                symbol, drop_pct, peak, current_price,
            )
            place_sell(symbol)
            peaks.pop(symbol, None)
        else:
            behind_pct = (peak - current_price) / peak * 100
            log.debug(
                "Trailing stop: %s $%.2f (peak $%.2f, %.1f%% behind)",
                symbol, current_price, peak, behind_pct,
            )

    state["peaks"] = peaks
    return state


def check_time_stops(state: dict, max_hold_days: int = 90) -> dict:
    """
    Exit positions that are:
      - held longer than max_hold_days, AND
      - flat or underwater (unrealized P&L <= +5%), AND
      - have no recent politician buy signal in the last max_hold_days days

    Profitable positions (>+5%) are left alone to run regardless of age.
    """
    client = get_client()
    entries: dict      = state.setdefault("position_entries", {})
    recent_buys: dict  = state.get("recent_buys", {})

    try:
        positions = client.get_all_positions()
    except Exception as exc:
        log.error("Could not fetch positions for time-stop check: %s", exc)
        return state

    today   = datetime.now(timezone.utc).date()
    cutoff  = (today - timedelta(days=max_hold_days)).isoformat()

    for pos in positions:
        symbol = pos.symbol
        if symbol == BASE_HOLDING:
            continue

        entry = entries.get(symbol)
        if not entry:
            continue  # no entry record (pre-dates this feature) — skip

        entry_date = entry.get("date", "9999-01-01")
        if entry_date > cutoff:
            continue  # not old enough yet

        # Skip if there's been a fresh buy signal from any politician
        fresh_signals = [
            e for e in recent_buys.get(symbol, [])
            if e.get("date", "0") >= cutoff
        ]
        if fresh_signals:
            log.info(
                "Time-stop: %s is old but has fresh signal from %s — holding",
                symbol, fresh_signals[0]["politician"],
            )
            continue

        # Skip if the position is running well (> +5% unrealized)
        try:
            unrealized_pct = float(pos.unrealized_plpc)
        except (AttributeError, TypeError, ValueError):
            unrealized_pct = 0.0

        if unrealized_pct > 0.05:
            log.info(
                "Time-stop: %s is old but up %.1f%% — letting it run",
                symbol, unrealized_pct * 100,
            )
            continue

        age_days = (today - datetime.strptime(entry_date, "%Y-%m-%d").date()).days
        log.info(
            "TIME-STOP: %s held %d days, P&L %.1f%%, no fresh signal — exiting",
            symbol, age_days, unrealized_pct * 100,
        )
        place_sell(symbol)
        entries.pop(symbol, None)

    state["position_entries"] = entries
    return state
