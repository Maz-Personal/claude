"""
Wheel Strategy — Paper Trading (Multi-Ticker)
══════════════════════════════════════════════
Usage:
    python wheel.py AMD BMY AAPL          ← run wheel on multiple tickers
    python wheel.py AMD                   ← run wheel on a single ticker
    python wheel.py AMD --contracts 2     ← override default contract count
    python wheel.py AMD BMY --otm 0.08    ← use 8% OTM instead of 10%

Strategy:
    Stage 1: Sell cash-secured puts  (~10% OTM, 2-4 weeks)
    Stage 2: Sell covered calls      (~10% above cost basis, 2-4 weeks)

Rules:
    - Close any contract early at 50% profit, then sell a fresh one
    - Never sell a put without enough cash to cover assignment
    - Never sell a call below cost basis
    - Track total premium collected across all cycles per ticker
    - Daily summary at 3:55 PM ET
    - Do nothing outside market hours
"""

import os
import sys
import json
import time
import argparse
import requests
from datetime import date, datetime, timedelta
from dotenv import dotenv_values

# ── Alpaca credentials ────────────────────────────────────────────────────────
#    Uses WHEEL_ALPACA_API_KEY / WHEEL_ALPACA_API_SECRET from .env
#    Falls back to ALPACA_API_KEY / ALPACA_API_SECRET if not set
creds      = dotenv_values(".env")
API_KEY    = creds.get("WHEEL_ALPACA_API_KEY", creds.get("ALPACA_API_KEY", ""))
API_SECRET = creds.get("WHEEL_ALPACA_API_SECRET", creds.get("ALPACA_API_SECRET", ""))
HEADERS    = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}
BASE       = "https://paper-api.alpaca.markets/v2"
DATA       = "https://data.alpaca.markets"

# ── Default strategy parameters (can be overridden via CLI) ───────────────────
DEFAULTS = {
    "contracts":        1,          # number of option contracts per trade
    "otm_pct":          0.10,       # 10% out of the money
    "early_close_pct":  0.50,       # close at 50% profit
    "poll_secs":        900,        # 15-minute polling interval
    "buying_power_cap": 500_000,    # max cash committed across all short puts
}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(symbol, msg):
    print(f"  [{ts()}] [{symbol}] {msg}")

def api_get(path, params=None, base=BASE):
    r = requests.get(f"{base}{path}", headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()

def api_post(path, body):
    r = requests.post(f"{BASE}{path}", headers=HEADERS, json=body)
    r.raise_for_status()
    return r.json()

def api_delete(path):
    requests.delete(f"{BASE}{path}", headers=HEADERS)

def is_market_hours():
    now_et = datetime.utcnow() - timedelta(hours=4)
    if now_et.weekday() >= 5:
        return False
    mins = now_et.hour * 60 + now_et.minute
    return (9 * 60 + 30) <= mins < (16 * 60)

def is_near_close():
    now_et = datetime.utcnow() - timedelta(hours=4)
    mins = now_et.hour * 60 + now_et.minute
    return mins >= (15 * 60 + 55)

def get_account():
    return api_get("/account")


# ══════════════════════════════════════════════════════════════════════════════
#  STATE MANAGEMENT — one state file per ticker
# ══════════════════════════════════════════════════════════════════════════════

def state_file(symbol):
    return f"{symbol.lower()}_wheel_state.json"

def load_state(symbol):
    path = state_file(symbol)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {
        "symbol":           symbol,
        "stage":            "PUT",
        "current_contract": None,
        "sold_premium":     0.0,
        "cost_basis":       None,
        "shares_owned":     0,
        "total_premium":    0.0,
        "cycles":           0,
        "history":          [],
    }

def save_state(state):
    with open(state_file(state["symbol"]), "w") as f:
        json.dump(state, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════

def get_stock_price(symbol):
    r = api_get(f"/v2/stocks/{symbol}/trades/latest", base=DATA)
    return float(r["trade"]["p"])

def get_option_quote(opt_symbol):
    r = requests.get(
        f"{DATA}/v1beta1/options/quotes/latest",
        headers=HEADERS, params={"symbols": opt_symbol},
    )
    if not r.ok:
        return 0.0, 0.0, 0.0
    q = r.json().get("quotes", {}).get(opt_symbol, {})
    bid = float(q.get("bp", 0))
    ask = float(q.get("ap", 0))
    mid = round((bid + ask) / 2, 2) if ask > 0 else 0.0
    return bid, ask, mid


# ══════════════════════════════════════════════════════════════════════════════
#  CONTRACT SELECTION
# ══════════════════════════════════════════════════════════════════════════════

def find_contract(symbol, contract_type, target_strike, weeks_min=2, weeks_max=4):
    exp_min = (date.today() + timedelta(weeks=weeks_min)).isoformat()
    exp_max = (date.today() + timedelta(weeks=weeks_max)).isoformat()
    r = api_get("/options/contracts", params={
        "underlying_symbols": symbol,
        "type":               contract_type,
        "expiration_date_gte": exp_min,
        "expiration_date_lte": exp_max,
        "strike_price_gte":   str(int(target_strike) - 20),
        "strike_price_lte":   str(int(target_strike) + 20),
        "limit":              20,
    })
    contracts = r.get("option_contracts", [])
    if not contracts:
        return None
    contracts.sort(key=lambda c: (
        abs(float(c["strike_price"]) - target_strike),
        c["expiration_date"],
    ))
    return contracts[0]


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER PLACEMENT
# ══════════════════════════════════════════════════════════════════════════════

def sell_to_open(opt_symbol, limit_price, contracts):
    return api_post("/orders", {
        "symbol":        opt_symbol,
        "qty":           str(contracts),
        "side":          "sell",
        "type":          "limit",
        "time_in_force": "day",
        "limit_price":   str(round(limit_price, 2)),
    })

def buy_to_close(opt_symbol, limit_price, contracts):
    return api_post("/orders", {
        "symbol":        opt_symbol,
        "qty":           str(contracts),
        "side":          "buy",
        "type":          "limit",
        "time_in_force": "day",
        "limit_price":   str(round(limit_price, 2)),
    })


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION CHECKS
# ══════════════════════════════════════════════════════════════════════════════

def get_option_position(opt_symbol):
    try:
        return api_get(f"/positions/{requests.utils.quote(opt_symbol, safe='')}")
    except Exception:
        return None

def get_stock_position(symbol):
    try:
        positions = api_get("/positions")
        return next(
            (p for p in positions
             if p.get("symbol") == symbol and p.get("asset_class") == "us_equity"),
            None,
        )
    except Exception:
        return None

def get_committed_cash():
    """Total cash committed by all open short put positions."""
    try:
        positions = api_get("/positions")
        total = 0.0
        for p in positions:
            sym = p.get("symbol", "")
            if (p.get("asset_class") == "us_option"
                    and "P" in sym
                    and float(p.get("qty", 0)) < 0):
                strike = int(sym[-8:]) / 1000
                qty = abs(float(p["qty"]))
                total += strike * 100 * qty
        return total
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 1 — SELL CASH-SECURED PUT
# ══════════════════════════════════════════════════════════════════════════════

def do_sell_put(state, cfg):
    symbol    = state["symbol"]
    contracts = cfg["contracts"]
    price     = get_stock_price(symbol)
    account   = get_account()
    cash      = float(account["cash"])

    target = round(price * (1 - cfg["otm_pct"]))
    needed = target * 100 * contracts

    if cash < needed:
        log(symbol, f"SKIP PUT — need ${needed:,.0f}, only ${cash:,.0f} cash")
        return state

    committed = get_committed_cash()
    if committed + needed > cfg["buying_power_cap"]:
        log(symbol, f"SKIP PUT — buying power cap: ${committed:,.0f} + ${needed:,.0f} > ${cfg['buying_power_cap']:,.0f}")
        return state

    contract = find_contract(symbol, "put", target)
    if not contract:
        log(symbol, "No put contracts in 2-4 week window — retry next poll")
        return state

    sym    = contract["symbol"]
    strike = float(contract["strike_price"])
    exp    = contract["expiration_date"]
    bid, ask, mid = get_option_quote(sym)

    if mid <= 0:
        log(symbol, f"No quote for {sym} — retry next poll")
        return state

    order = sell_to_open(sym, mid, contracts)
    state.update({"current_contract": sym, "sold_premium": mid, "stage": "PUT"})
    save_state(state)

    print()
    log(symbol, f">>> SOLD PUT  {sym}")
    log(symbol, f"    Strike: ${strike}  ({(strike/price - 1)*100:.1f}% OTM)  Exp: {exp}")
    log(symbol, f"    Bid: ${bid}  Ask: ${ask}  Limit: ${mid}  Credit: ${mid * 100 * contracts:.2f}")
    log(symbol, f"    Cash secured: ${needed:,.0f}  Order: {order.get('id')}  Status: {order.get('status')}")
    return state


# ══════════════════════════════════════════════════════════════════════════════
#  STAGE 2 — SELL COVERED CALL
# ══════════════════════════════════════════════════════════════════════════════

def do_sell_call(state, cfg):
    symbol     = state["symbol"]
    contracts  = cfg["contracts"]
    price      = get_stock_price(symbol)
    cost_basis = state["cost_basis"]

    target = round(cost_basis * (1 + cfg["otm_pct"]))
    if target < cost_basis:
        target = round(cost_basis * 1.05)

    contract = find_contract(symbol, "call", target)
    if not contract:
        log(symbol, "No call contracts in 2-4 week window — retry next poll")
        return state

    sym    = contract["symbol"]
    strike = float(contract["strike_price"])
    exp    = contract["expiration_date"]
    bid, ask, mid = get_option_quote(sym)

    if mid <= 0:
        log(symbol, f"No quote for {sym} — retry next poll")
        return state

    order = sell_to_open(sym, mid, contracts)
    state.update({"current_contract": sym, "sold_premium": mid, "stage": "CALL"})
    save_state(state)

    print()
    log(symbol, f">>> SOLD CALL  {sym}")
    log(symbol, f"    Strike: ${strike}  ({(strike/cost_basis - 1)*100:.1f}% above cost basis ${cost_basis})  Exp: {exp}")
    log(symbol, f"    Bid: ${bid}  Ask: ${ask}  Limit: ${mid}  Credit: ${mid * 100 * contracts:.2f}")
    log(symbol, f"    Stock price: ${price:.2f}  Order: {order.get('id')}  Status: {order.get('status')}")
    return state


# ══════════════════════════════════════════════════════════════════════════════
#  MONITOR EXISTING CONTRACT
# ══════════════════════════════════════════════════════════════════════════════

def do_monitor(state, cfg):
    symbol    = state["symbol"]
    contracts = cfg["contracts"]
    sym       = state["current_contract"]
    stage     = state["stage"]
    sold_at   = state["sold_premium"]
    price     = get_stock_price(symbol)

    position = get_option_position(sym)

    # ── Contract gone (expired or exercised) ──────────────────────────────────
    if position is None:
        log(symbol, f"Contract {sym} no longer active")

        if stage == "PUT":
            stock = get_stock_position(symbol)
            if stock:
                shares    = int(float(stock["qty"]))
                avg_entry = float(stock["avg_entry_price"])
                eff_basis = round(avg_entry - state["total_premium"] / max(shares, 1), 2)
                state.update({
                    "stage":            "CALL",
                    "shares_owned":     shares,
                    "cost_basis":       eff_basis,
                    "current_contract": None,
                })
                save_state(state)
                log(symbol, f"*** ASSIGNED ***  Bought {shares} {symbol} @ ${avg_entry:.2f}")
                log(symbol, f"    Effective cost basis (after premiums): ${eff_basis:.2f}")
            else:
                kept = sold_at * 100 * contracts
                state["total_premium"] += kept
                state["current_contract"] = None
                save_state(state)
                log(symbol, f"Put expired WORTHLESS — kept ${kept:.2f}  |  Total premium: ${state['total_premium']:.2f}")

        elif stage == "CALL":
            stock = get_stock_position(symbol)
            if stock is None:
                kept = sold_at * 100 * contracts
                state["total_premium"] += kept
                state["cycles"]        += 1
                state.update({
                    "stage":            "PUT",
                    "current_contract": None,
                    "shares_owned":     0,
                    "cost_basis":       None,
                })
                save_state(state)
                log(symbol, f"*** CALLED AWAY ***  Cycle {state['cycles']} complete")
                log(symbol, f"    Total premium collected: ${state['total_premium']:.2f}")
            else:
                kept = sold_at * 100 * contracts
                state["total_premium"] += kept
                state["current_contract"] = None
                save_state(state)
                log(symbol, f"Call expired WORTHLESS — kept ${kept:.2f}  |  Total premium: ${state['total_premium']:.2f}")
        return state

    # ── Contract still open — check for early close ───────────────────────────
    bid, ask, current_mid = get_option_quote(sym)
    if current_mid <= 0:
        log(symbol, f"Monitoring {sym}  stock=${price:.2f}  (no quote)")
        return state

    profit_pct = (sold_at - current_mid) / sold_at if sold_at > 0 else 0
    log(symbol, f"Monitoring [{stage}]  {sym}  sold=${sold_at:.2f}  now=${current_mid:.2f}  "
                f"profit={profit_pct:.1%}  stock=${price:.2f}")

    if profit_pct >= cfg["early_close_pct"]:
        order  = buy_to_close(sym, current_mid, contracts)
        profit = (sold_at - current_mid) * 100 * contracts
        state["total_premium"] += profit
        state["current_contract"] = None
        save_state(state)
        log(symbol, f"*** {cfg['early_close_pct']:.0%} PROFIT — CLOSING EARLY ***  "
                    f"Bought back @ ${current_mid:.2f}  Locked: ${profit:.2f}")
        log(symbol, f"    Total premium: ${state['total_premium']:.2f}  Order: {order.get('id')}")

    return state


# ══════════════════════════════════════════════════════════════════════════════
#  PROCESS ONE TICKER (one iteration)
# ══════════════════════════════════════════════════════════════════════════════

def process_ticker(state, cfg):
    """Run one strategy cycle for a single ticker. Returns updated state."""
    if state["current_contract"] is None:
        if state["stage"] == "PUT":
            return do_sell_put(state, cfg)
        else:
            return do_sell_call(state, cfg)
    else:
        return do_monitor(state, cfg)


# ══════════════════════════════════════════════════════════════════════════════
#  DAILY SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def daily_summary(states):
    account = get_account()
    pv      = float(account.get("portfolio_value", 0))
    cash    = float(account.get("cash", 0))

    print()
    print("=" * 64)
    print(f"  DAILY SUMMARY — {date.today()}  {ts()}")
    print("=" * 64)

    total_premium_all = 0.0
    for state in states:
        symbol = state["symbol"]
        try:
            price = get_stock_price(symbol)
        except Exception:
            price = 0.0

        cost = f"${state['cost_basis']:.2f}" if state["cost_basis"] else "—"
        print(f"  {symbol:6s}  Stage: {state['stage']:4s}  "
              f"Shares: {state['shares_owned']:4d}  "
              f"Basis: {cost:>8s}  "
              f"Premium: ${state['total_premium']:>8.2f}  "
              f"Cycles: {state['cycles']}  "
              f"Price: ${price:.2f}")
        total_premium_all += state["total_premium"]

    print(f"  {'─' * 60}")
    print(f"  Total premium (all tickers): ${total_premium_all:,.2f}")
    print(f"  Cash             : ${cash:,.2f}")
    print(f"  Portfolio value  : ${pv:,.2f}")
    total_return = pv - 100_000
    print(f"  P&L vs start     : ${total_return:+,.2f}")
    print("=" * 64)
    print()


# ══════════════════════════════════════════════════════════════════════════════
#  CLI & MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Wheel Strategy — run on one or more tickers",
        usage="python wheel.py TICKER [TICKER ...] [options]",
    )
    parser.add_argument(
        "tickers", nargs="+", type=str,
        help="One or more stock ticker symbols (e.g. AMD BMY AAPL)",
    )
    parser.add_argument(
        "--contracts", type=int, default=DEFAULTS["contracts"],
        help=f"Contracts per trade (default: {DEFAULTS['contracts']})",
    )
    parser.add_argument(
        "--otm", type=float, default=DEFAULTS["otm_pct"],
        help=f"OTM percentage as decimal (default: {DEFAULTS['otm_pct']})",
    )
    parser.add_argument(
        "--early-close", type=float, default=DEFAULTS["early_close_pct"],
        help=f"Early close profit threshold (default: {DEFAULTS['early_close_pct']})",
    )
    parser.add_argument(
        "--poll", type=int, default=DEFAULTS["poll_secs"],
        help=f"Polling interval in seconds (default: {DEFAULTS['poll_secs']})",
    )
    parser.add_argument(
        "--cap", type=float, default=DEFAULTS["buying_power_cap"],
        help=f"Max buying power committed (default: {DEFAULTS['buying_power_cap']:,.0f})",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    tickers = [t.upper() for t in args.tickers]
    cfg = {
        "contracts":        args.contracts,
        "otm_pct":          args.otm,
        "early_close_pct":  args.early_close,
        "poll_secs":        args.poll,
        "buying_power_cap": args.cap,
    }

    print("=" * 64)
    print(f"  WHEEL STRATEGY — Paper Trading")
    print(f"  Tickers    : {', '.join(tickers)}")
    print(f"  Contracts  : {cfg['contracts']}  |  OTM: {cfg['otm_pct']:.0%}  |  "
          f"Early close: {cfg['early_close_pct']:.0%}")
    print(f"  Poll       : {cfg['poll_secs']}s  |  BP cap: ${cfg['buying_power_cap']:,.0f}")
    print(f"  Started    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 64)

    # Load state for every ticker
    states = [load_state(t) for t in tickers]
    last_summary_date = None

    while True:
        try:
            if not is_market_hours():
                log("—", "Market closed — sleeping 5 min")
                time.sleep(300)
                continue

            # Daily summary near close
            if is_near_close() and last_summary_date != date.today():
                daily_summary(states)
                last_summary_date = date.today()

            # Process each ticker in round-robin
            for i, state in enumerate(states):
                try:
                    states[i] = process_ticker(state, cfg)
                except Exception as e:
                    log(state["symbol"], f"ERROR: {e}")

            time.sleep(cfg["poll_secs"])

        except KeyboardInterrupt:
            print("\nStopped. All states saved.")
            break
        except Exception as e:
            log("—", f"LOOP ERROR: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
