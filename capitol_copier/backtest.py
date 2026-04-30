"""
Backtest: simulate Capitol Copier running for the past year.
Compares portfolio performance vs SPY (S&P 500).

Methodology:
  - Start with $100,000
  - On each politician BUY  → spend 5% of current cash at that day's open price
  - On each politician SELL → liquidate full position at that day's open price
  - Pelosi: all trades | Hern: energy tickers only
  - Benchmark: same $100k fully invested in SPY on day 1
"""

import sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import yfinance as yf

from scraper import get_recent_trades
from sectors import is_energy, is_tech, is_semis, is_financials, is_healthcare, is_defense
from config import TARGETS, TRADE_SIZE_PCT, BASE_HOLDING, BASE_HOLDING_PCT, CASH_RESERVE_PCT

# ── Settings ──────────────────────────────────────────────────────────────────
INITIAL_CASH   = 100_000.0
SIM_DAYS       = 365          # how many calendar days back to simulate
TODAY          = datetime.now(timezone.utc).date()
START_DATE     = TODAY - timedelta(days=SIM_DAYS)

# ── Helpers ───────────────────────────────────────────────────────────────────

SECTOR_CHECKS = {
    "energy":     is_energy,
    "tech":       is_tech,
    "semis":      is_semis,
    "financials": is_financials,
    "healthcare": is_healthcare,
    "defense":    is_defense,
}

def fetch_all_trades(target: dict, pages: int = 10) -> list[dict]:
    raw = get_recent_trades(target["id"], pages=pages)
    sector = target.get("sector_filter")
    if sector and sector in SECTOR_CHECKS:
        raw = [t for t in raw if SECTOR_CHECKS[sector](t["ticker"])]
    return raw


def parse_date(s: str):
    """Return a date object from 'YYYY-MM-DD', or None."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def get_price_series(tickers: list[str], start, end) -> dict[str, dict]:
    """Download daily open prices for all tickers + SPY. Returns {ticker: {date: price}}."""
    import math
    all_tickers = list(set(tickers + ["SPY"]))
    print(f"  Downloading prices for: {', '.join(sorted(all_tickers))}")

    prices: dict[str, dict] = {t: {} for t in all_tickers}

    # Download in one batch; fall back to per-ticker on failure
    try:
        raw = yf.download(all_tickers, start=str(start), end=str(end),
                          auto_adjust=True, progress=False)
        opens = raw["Open"] if hasattr(raw["Open"], "columns") else raw[["Open"]]
        for dt, row in opens.iterrows():
            d = dt.date()
            for t in all_tickers:
                try:
                    v = float(row[t])
                    if not math.isnan(v) and v > 0:
                        prices[t][d] = v
                except (KeyError, TypeError, ValueError):
                    pass
    except Exception as exc:
        print(f"  Batch download failed ({exc}), falling back to per-ticker...")

    # Fill in any tickers that came back empty individually
    missing = [t for t in all_tickers if not prices[t]]
    for t in missing:
        try:
            df = yf.download(t, start=str(start), end=str(end),
                             auto_adjust=True, progress=False)
            for dt, row in df["Open"].items():
                try:
                    v = float(row)
                    if not math.isnan(v) and v > 0:
                        prices[t][dt.date()] = v
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass

    coverage = {t: len(v) for t, v in prices.items() if v}
    print(f"  Price coverage: {coverage}")
    return prices


def nearest_price(price_series: dict, date, direction: str = "forward") -> float | None:
    """Get price on date or the nearest trading day within 7 days."""
    offsets = range(7) if direction == "forward" else range(0, -8, -1)
    for offset in offsets:
        p = price_series.get(date + timedelta(days=offset))
        if p:
            return p
    return None


# ── Collect and filter trades ─────────────────────────────────────────────────

def collect_trades() -> list[dict]:
    all_trades = []
    seen_ids: set[str] = set()

    for target in TARGETS:
        print(f"  Fetching trades: {target['name']} ({target.get('sector_filter') or 'all sectors'})...")
        trades = fetch_all_trades(target, pages=10)
        for t in trades:
            t["_politician"] = target["name"]
        all_trades.extend(trades)

    # Deduplicate by trade_id, filter to window and valid actions
    filtered = []
    for t in all_trades:
        tid = t.get("trade_id", "")
        if tid and tid in seen_ids:
            continue
        if tid:
            seen_ids.add(tid)

        d = parse_date(t.get("pub_date", ""))
        if d is None or d < START_DATE or d > TODAY:
            continue
        if t["action"] not in ("buy", "sell"):
            continue
        t["_date"] = d
        filtered.append(t)

    filtered.sort(key=lambda x: x["_date"])
    return filtered


# ── Simulation ────────────────────────────────────────────────────────────────

def portfolio_equity(cash: float, holdings: dict, prices: dict, date) -> float:
    total = cash
    for ticker, pos in holdings.items():
        p = nearest_price(prices.get(ticker, {}), date, direction="backward")
        if p:
            total += pos["qty"] * p
    return total


def simulate(trades: list[dict], prices: dict[str, dict]) -> dict:
    """
    Improved simulation:
      - Start: deploy BASE_HOLDING_PCT into SPY on day 1
      - BUY signal: size at TRADE_SIZE_PCT of total equity; trim SPY if short on cash
      - SELL signal: liquidate stock, recycle into SPY
    """
    cash      = INITIAL_CASH
    holdings  = {}   # {ticker: {"qty": float, "cost": float}}
    trade_log = []

    def current_equity(date):
        return portfolio_equity(cash, holdings, prices, date)

    def holding_value(ticker, date):
        if ticker not in holdings:
            return 0.0
        p = nearest_price(prices.get(ticker, {}), date)
        return holdings[ticker]["qty"] * p if p else 0.0

    def buy_ticker(ticker, notional, date, label):
        nonlocal cash
        price = nearest_price(prices.get(ticker, {}), date)
        if not price:
            return
        actual = min(notional, cash)
        if actual < 1.0:
            return
        qty = actual / price
        cash -= actual
        if ticker in holdings:
            h = holdings[ticker]
            holdings[ticker] = {"qty": h["qty"] + qty, "cost": h["cost"] + actual}
        else:
            holdings[ticker] = {"qty": qty, "cost": actual}
        trade_log.append({
            "date": str(date), "action": "BUY", "ticker": ticker,
            "qty": qty, "price": price, "spend": actual, "politician": label,
        })

    def sell_ticker(ticker, date, label, notional=None):
        nonlocal cash
        if ticker not in holdings:
            return 0.0
        price = nearest_price(prices.get(ticker, {}), date, direction="backward")
        if not price:
            return 0.0
        pos = holdings[ticker]
        if notional:
            # partial sell (used for trimming SPY)
            qty = min(notional / price, pos["qty"])
        else:
            qty = pos["qty"]
        proceeds = qty * price
        gain     = proceeds - (pos["cost"] * qty / pos["qty"])
        cash    += proceeds
        if qty >= pos["qty"] - 1e-9:
            del holdings[ticker]
        else:
            holdings[ticker] = {
                "qty":  pos["qty"]  - qty,
                "cost": pos["cost"] - (pos["cost"] * qty / pos["qty"]),
            }
        trade_log.append({
            "date": str(date), "action": "SELL", "ticker": ticker,
            "qty": qty, "price": price, "proceeds": proceeds,
            "gain": gain, "politician": label,
        })
        return proceeds

    # ── Day 1: buy initial SPY base position ──────────────────────────────────
    first_date = trades[0]["_date"] if trades else START_DATE
    spy_target = round(INITIAL_CASH * BASE_HOLDING_PCT, 2)
    buy_ticker(BASE_HOLDING, spy_target, first_date, "Base")

    # ── Process each politician trade ─────────────────────────────────────────
    for trade in trades:
        date   = trade["_date"]
        ticker = trade["ticker"]
        action = trade["action"]
        pol    = trade["_politician"]

        if ticker == BASE_HOLDING:
            continue  # never copy a direct SPY trade as a signal

        if action == "buy":
            equity   = current_equity(date)
            notional = round(equity * TRADE_SIZE_PCT, 2)
            reserve  = equity * CASH_RESERVE_PCT

            shortfall = notional - max(0.0, cash - reserve)
            if shortfall > 0:
                # trim SPY to fund
                spy_val = holding_value(BASE_HOLDING, date)
                trim    = min(shortfall, spy_val * 0.99)
                if trim > 1.0:
                    sell_ticker(BASE_HOLDING, date, "Base (trim)", notional=trim)

            buy_ticker(ticker, notional, date, pol)

        elif action == "sell":
            if ticker in holdings:
                proceeds = sell_ticker(ticker, date, pol)
                # recycle proceeds back into SPY
                equity   = current_equity(date)
                spy_val  = holding_value(BASE_HOLDING, date)
                spy_gap  = equity * BASE_HOLDING_PCT - spy_val
                rebuy    = round(min(spy_gap, max(0.0, cash - equity * CASH_RESERVE_PCT)), 2)
                if rebuy > 1.0:
                    buy_ticker(BASE_HOLDING, rebuy, date, "Base (recycle)")

    # ── Final valuation ───────────────────────────────────────────────────────
    final_equity   = portfolio_equity(cash, holdings, prices, TODAY)
    open_positions = {}
    for ticker, pos in holdings.items():
        p = nearest_price(prices.get(ticker, {}), TODAY, direction="backward")
        if p:
            mv = pos["qty"] * p
            open_positions[ticker] = {
                "qty": pos["qty"], "cost": pos["cost"],
                "market_value": mv, "unrealized_pnl": mv - pos["cost"],
            }

    return {
        "final_equity": final_equity,
        "cash": cash,
        "open_positions": open_positions,
        "trade_log": trade_log,
        "total_return_pct": (final_equity - INITIAL_CASH) / INITIAL_CASH * 100,
    }


# ── SPY benchmark ─────────────────────────────────────────────────────────────

def spy_benchmark(prices: dict[str, dict]) -> dict:
    spy = prices.get("SPY", {})
    start_price = nearest_price(spy, START_DATE, direction="forward")
    end_price   = nearest_price(spy, TODAY, direction="backward")
    if not start_price or not end_price:
        return {"return_pct": None, "final_value": None}
    shares = INITIAL_CASH / start_price
    final  = shares * end_price
    return {
        "start_price": start_price,
        "end_price":   end_price,
        "final_value": final,
        "return_pct":  (final - INITIAL_CASH) / INITIAL_CASH * 100,
    }


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(result: dict, spy: dict, trades: list[dict]):
    sep = "=" * 62

    print(f"\n{sep}")
    print(f"  BACKTEST: {START_DATE} -> {TODAY}  ({SIM_DAYS} days)")
    print(f"  Starting capital: ${INITIAL_CASH:,.0f}")
    print(sep)

    # Bot performance
    ret  = result["total_return_pct"]
    sign = "+" if ret >= 0 else ""
    print(f"\n  CAPITOL COPIER BOT")
    print(f"    Final equity:   ${result['final_equity']:>12,.2f}")
    print(f"    Cash remaining: ${result['cash']:>12,.2f}")
    print(f"    Total return:   {sign}{ret:.2f}%")
    print(f"    Trades copied:  {len(result['trade_log'])}")

    # Open positions
    if result["open_positions"]:
        print(f"\n    Open positions:")
        for t, p in sorted(result["open_positions"].items(),
                           key=lambda x: -x[1]["market_value"]):
            pnl  = p["unrealized_pnl"]
            sign2 = "+" if pnl >= 0 else ""
            print(f"      {t:6s}  MV ${p['market_value']:>10,.2f}  "
                  f"P&L {sign2}${pnl:,.2f}")

    # SPY benchmark
    print(f"\n  S&P 500 (SPY) BENCHMARK")
    spy_ret = spy.get("return_pct")
    if spy_ret is not None:
        spy_sign = "+" if spy_ret >= 0 else ""
        print(f"    Buy price ({START_DATE}): ${spy['start_price']:,.2f}")
        print(f"    Price today:    ${spy['end_price']:,.2f}")
        print(f"    Final value:    ${spy['final_value']:>12,.2f}")
        print(f"    Total return:   {spy_sign}{spy_ret:.2f}%")
        delta = result["total_return_pct"] - spy_ret
        delta_sign = "+" if delta >= 0 else ""
        verdict = "OUTPERFORMED" if delta >= 0 else "UNDERPERFORMED"
        print(f"\n  ALPHA vs SPY:     {delta_sign}{delta:.2f}%  ({verdict})")
    else:
        print("    (SPY price data unavailable)")

    # Trade breakdown by politician
    print(f"\n  TRADES BY POLITICIAN")
    by_pol = defaultdict(list)
    for t in result["trade_log"]:
        by_pol[t["politician"]].append(t)
    for pol, pol_trades in by_pol.items():
        buys  = [t for t in pol_trades if t["action"] == "BUY"]
        sells = [t for t in pol_trades if t["action"] == "SELL"]
        realized = sum(t.get("gain", 0) for t in sells)
        sign3 = "+" if realized >= 0 else ""
        print(f"    {pol}")
        print(f"      Buys: {len(buys)}  Sells: {len(sells)}  "
              f"Realized P&L: {sign3}${realized:,.2f}")

    # Top 10 trades
    if result["trade_log"]:
        print(f"\n  TRADE LOG (all executed trades)")
        print(f"    {'Date':<12} {'Act':<5} {'Ticker':<7} {'Qty':>8} "
              f"{'Price':>8} {'$In/Out':>12} {'Politician'}")
        print(f"    {'-'*12} {'-'*5} {'-'*7} {'-'*8} {'-'*8} {'-'*12} {'-'*20}")
        for t in result["trade_log"]:
            amt = t.get("spend") or t.get("proceeds") or 0
            print(f"    {t['date']:<12} {t['action']:<5} {t['ticker']:<7} "
                  f"{t['qty']:>8.3f} {t['price']:>8.2f} "
                  f"${amt:>10,.2f}  {t['politician']}")

    print(f"\n{sep}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\nCapitol Copier — Backtest Simulation")
    print(f"Period: {START_DATE} to {TODAY}\n")

    print("Step 1: Collecting trades from Capitol Trades...")
    trades = collect_trades()
    print(f"  Found {len(trades)} trade(s) in simulation window\n")

    if not trades:
        print(f"  No trades found in the window {START_DATE} → {TODAY}.")
        print("  This likely means the SSR snapshot on Capitol Trades doesn't")
        print("  yet show disclosures for this period. Widening to all available data...\n")

        # Fallback: use all available data regardless of date
        all_trades = []
        for target in TARGETS:
            raw = fetch_all_trades(target, pages=10)
            for t in raw:
                t["_politician"] = target["name"]
                d = parse_date(t.get("pub_date", ""))
                if d and t["action"] in ("buy", "sell"):
                    t["_date"] = d
                    all_trades.append(t)
        trades = sorted(all_trades, key=lambda x: x["_date"])
        if trades:
            start_actual = trades[0]["_date"]
            end_actual   = trades[-1]["_date"]
            print(f"  Using available range: {start_actual} → {end_actual}")
            print(f"  Found {len(trades)} trade(s)\n")
            START_DATE = start_actual

    if not trades:
        print("No trade data available. Exiting.")
        sys.exit(0)

    tickers = list({t["ticker"] for t in trades})
    print(f"Step 2: Downloading historical prices ({len(tickers)} tickers + SPY)...")
    prices = get_price_series(tickers, START_DATE - timedelta(days=10), TODAY + timedelta(days=1))
    print()

    print("Step 3: Running simulation...")
    result = simulate(trades, prices)
    spy    = spy_benchmark(prices)

    print_report(result, spy, trades)
