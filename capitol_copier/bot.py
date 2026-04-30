"""
Main bot loop: scrape Capitol Trades -> execute new trades via Alpaca.
Supports multiple copy targets with optional per-target sector filters.
Run on a schedule (every 30 min via Windows Task Scheduler).

Active improvements:
  - Trailing stop (10% from peak)
  - Time-stop: exit flat/losing positions held 90+ days with no fresh signal
  - 20% single-position cap (enforced in trader.py)
  - Market regime filter: skip all buys when SPY is below 200-day MA
  - Earnings blackout: skip buys within 7 days of earnings
  - Filing-speed multiplier: faster disclosure = larger size
  - Confluence: 2+ politicians buying same ticker in 30 days = 1.5x size
  - Options signal: politician buys options -> buy the underlying at 1.25x
  - Partial sell: mirror the politician's sell fraction rather than always exiting full
"""

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import MAX_TRADE_AGE_DAYS, STATE_FILE, LOG_FILE, TARGETS
from market import is_bull_market, has_earnings_soon
from scraper import get_recent_trades, filter_new_trades
from sectors import is_energy, is_tech, is_semis, is_financials, is_healthcare, is_defense
from stops import check_trailing_stops, check_time_stops
from trader import execute_trade, get_account, rebalance_to_base, place_partial_sell, get_position_value

# ── Logging with rotation ─────────────────────────────────────────────────────
# Rotates at 5 MB, keeps 5 backup files → max ~25 MB of logs on disk
_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
log = logging.getLogger(__name__)

_BOT_DIR   = Path(__file__).parent
STATE_PATH = _BOT_DIR / STATE_FILE
_LOG_PATH  = _BOT_DIR / LOG_FILE

SECTOR_CHECKS = {
    "energy":     is_energy,
    "tech":       is_tech,
    "semis":      is_semis,
    "financials": is_financials,
    "healthcare": is_healthcare,
    "defense":    is_defense,
}

CONFLUENCE_WINDOW_DAYS = 30
CONFLUENCE_MULT        = 1.5
OPTIONS_MULT           = 1.25


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {
        "seen_trade_ids":    [],
        "executed":          [],
        "last_run":          None,
        "peaks":             {},
        "recent_buys":       {},
        "position_entries":  {},
    }


def save_state(state: dict):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def _prune_recent_buys(recent_buys: dict) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CONFLUENCE_WINDOW_DAYS)).date().isoformat()
    return {
        ticker: [e for e in entries if e.get("date", "9999") >= cutoff]
        for ticker, entries in recent_buys.items()
        if any(e.get("date", "9999") >= cutoff for e in entries)
    }


# ── Multiplier helpers ────────────────────────────────────────────────────────

def filing_speed_mult(trade: dict) -> float:
    """
    Faster filers have better information — weight accordingly.
      < 5 days   -> 1.30x
      5-15 days  -> 1.00x
      16-30 days -> 0.85x
      > 30 days  -> 0.70x
    """
    try:
        pub  = datetime.strptime(trade["pub_date"],   "%Y-%m-%d")
        trd  = datetime.strptime(trade["trade_date"], "%Y-%m-%d")
        days = max(0, (pub - trd).days)
    except (ValueError, KeyError):
        return 1.0

    if days < 5:
        mult = 1.30
    elif days <= 15:
        mult = 1.00
    elif days <= 30:
        mult = 0.85
    else:
        mult = 0.70

    log.info("Filing speed: %d days -> %.2fx", days, mult)
    return mult


def confluence_mult(ticker: str, pol_name: str, recent_buys: dict) -> float:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CONFLUENCE_WINDOW_DAYS)).date().isoformat()
    others = [
        e for e in recent_buys.get(ticker, [])
        if e["politician"] != pol_name and e.get("date", "0") >= cutoff
    ]
    if others:
        names = ", ".join(e["politician"] for e in others)
        log.info("Confluence: %s also bought by %s -> %.1fx", ticker, names, CONFLUENCE_MULT)
        return CONFLUENCE_MULT
    return 1.0


def record_buy(ticker: str, pol_name: str, recent_buys: dict):
    today = datetime.now(timezone.utc).date().isoformat()
    recent_buys.setdefault(ticker, []).append({"politician": pol_name, "date": today})


# ── Partial sell helpers ──────────────────────────────────────────────────────

def _sell_fraction(trade: dict, pol_name: str, state: dict) -> float:
    sell_amount = trade.get("amount", 0.0)
    if sell_amount <= 0:
        return 1.0

    ticker = trade["ticker"]
    matching_buys = [
        e for e in state.get("executed", [])
        if e.get("politician") == pol_name
        and e.get("symbol") == ticker
        and e.get("side") == "buy"
        and e.get("source_amount", 0) > 0
    ]
    if not matching_buys:
        return 1.0

    buy_amount = matching_buys[-1]["source_amount"]
    fraction   = min(1.0, sell_amount / buy_amount)
    log.info(
        "Partial sell %s: politician filed $%.0f sell vs $%.0f buy -> %.0f%% of our position",
        ticker, sell_amount, buy_amount, fraction * 100,
    )
    return fraction


# ── Sector filter ─────────────────────────────────────────────────────────────

def apply_sector_filter(trades: list[dict], sector: str | None) -> list[dict]:
    if sector is None:
        return trades
    check = SECTOR_CHECKS.get(sector)
    if check is None:
        log.warning("Unknown sector filter '%s' — passing all trades through", sector)
        return trades
    kept    = [t for t in trades if check(t["ticker"])]
    skipped = len(trades) - len(kept)
    if skipped:
        log.info("Sector filter '%s': kept %d / %d trades", sector, len(kept), len(trades))
    return kept


# ── Per-target processing ─────────────────────────────────────────────────────

def process_target(
    target: dict,
    seen_ids: set[str],
    state: dict,
    bull_market: bool,
) -> list[dict]:
    pol_id   = target["id"]
    pol_name = target["name"]
    sector   = target.get("sector_filter")
    recent_buys:      dict = state.setdefault("recent_buys", {})
    position_entries: dict = state.setdefault("position_entries", {})

    label = f"{pol_name} [{sector or 'all sectors'}]"
    log.info("-- Checking %s --", label)

    raw = get_recent_trades(pol_id, pages=2)
    new = filter_new_trades(raw, seen_ids, MAX_TRADE_AGE_DAYS)
    new = apply_sector_filter(new, sector)

    # Convert option buys to underlying stock buys
    converted = []
    for t in new:
        if t.get("asset_type", "").lower() == "option" and t["action"] == "buy":
            log.info(
                "Options signal: %s bought %s option -> buying underlying at %.2fx",
                pol_name, t["ticker"], OPTIONS_MULT,
            )
            converted.append({**t, "asset_type": "Stock", "_options_mult": OPTIONS_MULT})
        else:
            converted.append({**t, "_options_mult": 1.0})
    new = converted

    log.info("%s: %d new trade(s) to process", pol_name, len(new))

    executed = []
    for trade in new:
        ticker = trade["ticker"]
        action = trade["action"]
        log.info("  %s %s (pub: %s)", action.upper(), ticker, trade.get("pub_date"))

        seen_ids.add(trade["_id"])

        if action == "buy":
            if not bull_market:
                log.info("  SKIPPED (bear market — SPY below 200-day MA)")
                continue

            if has_earnings_soon(ticker):
                log.info("  SKIPPED (earnings within 7 days)")
                continue

            speed_m    = filing_speed_mult(trade)
            conf_m     = confluence_mult(ticker, pol_name, recent_buys)
            opt_m      = trade.pop("_options_mult", 1.0)
            total_mult = round(speed_m * conf_m * opt_m, 4)

            if total_mult != 1.0:
                log.info(
                    "  Size multiplier: %.2fx (speed=%.2f conf=%.2f opt=%.2f)",
                    total_mult, speed_m, conf_m, opt_m,
                )

            result = execute_trade(trade, size_mult=total_mult)
            if result:
                record_buy(ticker, pol_name, recent_buys)
                today = datetime.now(timezone.utc).date().isoformat()
                position_entries.setdefault(ticker, {"date": today, "politicians": []})
                if pol_name not in position_entries[ticker]["politicians"]:
                    position_entries[ticker]["politicians"].append(pol_name)
                executed.append({
                    **result,
                    "politician":    pol_name,
                    "source_trade":  trade["_id"],
                    "source_amount": trade.get("amount", 0),
                    "copied_at":     datetime.now(timezone.utc).isoformat(),
                    "size_mult":     total_mult,
                })

        elif action == "sell":
            trade.pop("_options_mult", None)
            fraction = _sell_fraction(trade, pol_name, state)

            if fraction >= 0.95:
                result = execute_trade(trade)
                if result:
                    position_entries.pop(ticker, None)
            else:
                result = place_partial_sell(ticker, fraction)
                remaining = get_position_value(ticker)
                if remaining < 10.0:
                    position_entries.pop(ticker, None)

            if result:
                executed.append({
                    **result,
                    "politician":    pol_name,
                    "source_trade":  trade["_id"],
                    "copied_at":     datetime.now(timezone.utc).isoformat(),
                    "sell_fraction": fraction,
                })

        else:
            log.info("  Unrecognised action '%s' — skipping", action)

    return executed


# ── Main run ──────────────────────────────────────────────────────────────────

def run():
    log.info("=== Capitol Copier starting | targets: %s ===",
             ", ".join(f"{t['name']} ({t.get('sector_filter') or 'all'})" for t in TARGETS))

    try:
        acct = get_account()
        log.info("Alpaca: %s | cash $%s | equity $%s", acct.status, acct.cash, acct.equity)
    except Exception as exc:
        log.error("Cannot reach Alpaca — aborting: %s", exc)
        sys.exit(1)

    state = load_state()

    log.info("Checking trailing stops...")
    try:
        state = check_trailing_stops(state)
    except Exception as exc:
        log.warning("Trailing stop check failed: %s", exc)

    log.info("Checking time-stops...")
    try:
        state = check_time_stops(state)
    except Exception as exc:
        log.warning("Time-stop check failed: %s", exc)

    log.info("Checking market regime...")
    try:
        bull_market = is_bull_market()
    except Exception as exc:
        log.warning("Market regime check failed (%s) — assuming bull", exc)
        bull_market = True

    if not bull_market:
        log.warning("BEAR MARKET detected — all politician buys will be skipped this run")

    log.info("Rebalancing base SPY position...")
    try:
        rebalance_to_base()
    except Exception as exc:
        log.warning("Base rebalance skipped: %s", exc)

    seen_ids: set[str] = set(state.get("seen_trade_ids", []))
    state["recent_buys"] = _prune_recent_buys(state.get("recent_buys", {}))

    all_executed = []
    for target in TARGETS:
        try:
            executed = process_target(target, seen_ids, state, bull_market)
            all_executed.extend(executed)
        except Exception as exc:
            log.error("Error processing %s: %s", target["name"], exc)

    state["seen_trade_ids"] = list(seen_ids)
    state["executed"].extend(all_executed)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    log.info("=== Done. %d trade(s) executed this run. ===", len(all_executed))
    for t in all_executed:
        side     = t.get("side", "?").upper()
        mult     = t.get("size_mult", "")
        mult_str = f" (mult {mult:.2f}x)" if mult else ""
        log.info("  -> [%s] %s %s — order %s%s",
                 t["politician"], side, t["symbol"], t["id"], mult_str)


if __name__ == "__main__":
    run()
