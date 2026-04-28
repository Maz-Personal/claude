"""
SOUN Wheel Strategy — Paper Trading
Stage 1: Sell cash-secured puts  (~10% OTM, 2-4 weeks)
Stage 2: Sell covered calls      (~10% above cost basis, 2-4 weeks)
Rules:
  - Close any contract early at 50% profit, then sell a fresh one
  - Never sell a put without enough cash to cover assignment
  - Never sell a call below cost basis
  - Track total premium collected across all cycles
  - Daily summary at 3:55 PM ET
  - Do nothing outside market hours
"""

import os
import json
import time
import requests
from datetime import date, datetime, timedelta
from dotenv import dotenv_values

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOL          = "SOUN"
CONTRACTS       = 1          # number of option contracts per trade
OTM_PCT         = 0.10       # 10% out of the money
EARLY_CLOSE_PCT = 0.50       # close at 50% profit
POLL_SECS       = 900        # 15 minutes
STATE_FILE      = "soun_wheel_state.json"

creds      = dotenv_values(".env")
API_KEY    = creds["ALPACA_API_KEY"]
API_SECRET = creds["ALPACA_API_SECRET"]
HEADERS    = {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET}
BASE       = "https://paper-api.alpaca.markets/v2"
DATA       = "https://data.alpaca.markets"

# ── Helpers ───────────────────────────────────────────────────────────────────
def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(msg):
    print(f"  [{ts()}] {msg}")

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

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "stage":            "PUT",
        "current_contract": None,
        "sold_premium":     0.0,   # per-share premium received on current contract
        "cost_basis":       None,  # per-share cost (avg entry minus premiums earned)
        "shares_owned":     0,
        "total_premium":    0.0,   # cumulative dollar premium across all cycles
        "cycles":           0,
        "history":          [],
    }

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

# ── Market data ───────────────────────────────────────────────────────────────
def get_stock_price():
    r = api_get(f"/v2/stocks/{SYMBOL}/trades/latest", base=DATA)
    return float(r["trade"]["p"])

def get_option_quote(symbol):
    r = requests.get(
        f"{DATA}/v1beta1/options/quotes/latest",
        headers=HEADERS, params={"symbols": symbol}
    )
    if not r.ok:
        return 0.0, 0.0, 0.0
    q = r.json().get("quotes", {}).get(symbol, {})
    bid = float(q.get("bp", 0))
    ask = float(q.get("ap", 0))
    mid = round((bid + ask) / 2, 2) if ask > 0 else 0.0
    return bid, ask, mid

# ── Contract selection ────────────────────────────────────────────────────────
def find_contract(contract_type, target_strike, weeks_min=2, weeks_max=4):
    exp_min = (date.today() + timedelta(weeks=weeks_min)).isoformat()
    exp_max = (date.today() + timedelta(weeks=weeks_max)).isoformat()
    r = api_get("/options/contracts", params={
        "underlying_symbols": SYMBOL,
        "type": contract_type,
        "expiration_date_gte": exp_min,
        "expiration_date_lte": exp_max,
        "strike_price_gte": str(int(target_strike) - 20),
        "strike_price_lte": str(int(target_strike) + 20),
        "limit": 20,
    })
    contracts = r.get("option_contracts", [])
    if not contracts:
        return None
    # closest strike first, then earliest expiry
    contracts.sort(key=lambda c: (
        abs(float(c["strike_price"]) - target_strike),
        c["expiration_date"]
    ))
    return contracts[0]

# ── Order placement ───────────────────────────────────────────────────────────
def sell_to_open(symbol, limit_price):
    return api_post("/orders", {
        "symbol":          symbol,
        "qty":             str(CONTRACTS),
        "side":            "sell",
        "type":            "limit",
        "time_in_force":   "day",
        "limit_price":     str(round(limit_price, 2)),
    })

def buy_to_close(symbol, limit_price):
    return api_post("/orders", {
        "symbol":          symbol,
        "qty":             str(CONTRACTS),
        "side":            "buy",
        "type":            "limit",
        "time_in_force":   "day",
        "limit_price":     str(round(limit_price, 2)),
    })

# ── Position checks ───────────────────────────────────────────────────────────
def get_option_position(symbol):
    try:
        return api_get(f"/positions/{requests.utils.quote(symbol, safe='')}")
    except Exception:
        return None

def get_stock_position():
    try:
        positions = api_get("/positions")
        return next((p for p in positions
                     if p.get("symbol") == SYMBOL and p.get("asset_class") == "us_equity"), None)
    except Exception:
        return None

def get_account():
    return api_get("/account")

# ── Stage 1: sell cash-secured put ───────────────────────────────────────────
def do_sell_put(state):
    price   = get_stock_price()
    account = get_account()
    cash    = float(account["cash"])

    target  = round(price * (1 - OTM_PCT))
    needed  = target * 100 * CONTRACTS

    if cash < needed:
        log(f"SKIP PUT — need ${needed:,.0f} cash, only have ${cash:,.0f}")
        return state

    contract = find_contract("put", target)
    if not contract:
        log("No put contracts found in 2-4 week window — will retry next poll")
        return state

    sym    = contract["symbol"]
    strike = float(contract["strike_price"])
    exp    = contract["expiration_date"]
    bid, ask, mid = get_option_quote(sym)

    if mid <= 0:
        log(f"Could not get quote for {sym} — will retry next poll")
        return state

    order = sell_to_open(sym, mid)
    state.update({"current_contract": sym, "sold_premium": mid, "stage": "PUT"})
    save_state(state)

    print()
    log(f">>> SOLD PUT  {sym}")
    log(f"    Strike: ${strike}  ({(strike/price - 1)*100:.1f}% OTM)  Exp: {exp}")
    log(f"    Bid: ${bid}  Ask: ${ask}  Limit: ${mid}  Credit: ${mid * 100 * CONTRACTS:.2f}")
    log(f"    Cash secured: ${needed:,.0f}  Order: {order.get('id')}  Status: {order.get('status')}")
    return state

# ── Stage 2: sell covered call ────────────────────────────────────────────────
def do_sell_call(state):
    price      = get_stock_price()
    cost_basis = state["cost_basis"]

    target = round(cost_basis * (1 + OTM_PCT))
    if target < cost_basis:      # safety: never sell below cost basis
        target = round(cost_basis * 1.05)

    contract = find_contract("call", target)
    if not contract:
        log("No call contracts found in 2-4 week window — will retry next poll")
        return state

    sym    = contract["symbol"]
    strike = float(contract["strike_price"])
    exp    = contract["expiration_date"]
    bid, ask, mid = get_option_quote(sym)

    if mid <= 0:
        log(f"Could not get quote for {sym} — will retry next poll")
        return state

    order = sell_to_open(sym, mid)
    state.update({"current_contract": sym, "sold_premium": mid, "stage": "CALL"})
    save_state(state)

    print()
    log(f">>> SOLD CALL  {sym}")
    log(f"    Strike: ${strike}  ({(strike/cost_basis - 1)*100:.1f}% above cost basis ${cost_basis})  Exp: {exp}")
    log(f"    Bid: ${bid}  Ask: ${ask}  Limit: ${mid}  Credit: ${mid * 100 * CONTRACTS:.2f}")
    log(f"    Stock price: ${price:.2f}  Order: {order.get('id')}  Status: {order.get('status')}")
    return state

# ── Monitor existing contract ─────────────────────────────────────────────────
def do_monitor(state):
    sym      = state["current_contract"]
    stage    = state["stage"]
    sold_at  = state["sold_premium"]
    price    = get_stock_price()

    position = get_option_position(sym)

    # ── Contract gone (expired or exercised) ──────────────────────────────────
    if position is None:
        log(f"Contract {sym} no longer active")

        if stage == "PUT":
            stock = get_stock_position()
            if stock:
                shares    = int(float(stock["qty"]))
                avg_entry = float(stock["avg_entry_price"])
                # effective cost basis = avg entry minus all premium earned so far
                eff_basis = round(avg_entry - state["total_premium"] / max(shares, 1), 2)
                state.update({
                    "stage":        "CALL",
                    "shares_owned": shares,
                    "cost_basis":   eff_basis,
                    "current_contract": None,
                })
                save_state(state)
                log(f"*** ASSIGNED ***  Bought {shares} SOUN @ ${avg_entry:.2f}")
                log(f"    Effective cost basis (after premiums): ${eff_basis:.2f}")
            else:
                kept = sold_at * 100 * CONTRACTS
                state["total_premium"] += kept
                state["current_contract"] = None
                save_state(state)
                log(f"Put expired WORTHLESS — kept ${kept:.2f}  |  Total premium: ${state['total_premium']:.2f}")

        elif stage == "CALL":
            stock = get_stock_position()
            if stock is None:
                # shares were called away
                kept = sold_at * 100 * CONTRACTS
                state["total_premium"] += kept
                state["cycles"]        += 1
                state.update({"stage": "PUT", "current_contract": None,
                              "shares_owned": 0, "cost_basis": None})
                save_state(state)
                log(f"*** CALLED AWAY ***  Shares sold at strike  Cycle {state['cycles']} complete")
                log(f"    Total premium collected: ${state['total_premium']:.2f}")
            else:
                kept = sold_at * 100 * CONTRACTS
                state["total_premium"] += kept
                state["current_contract"] = None
                save_state(state)
                log(f"Call expired WORTHLESS — kept ${kept:.2f}  |  Total premium: ${state['total_premium']:.2f}")
        return state

    # ── Contract still open — check for 50% profit ───────────────────────────
    bid, ask, current_mid = get_option_quote(sym)
    if current_mid <= 0:
        log(f"Monitoring {sym}  stock=${price:.2f}  (no quote available)")
        return state

    profit_pct = (sold_at - current_mid) / sold_at if sold_at > 0 else 0
    log(f"Monitoring [{stage}]  {sym}  sold=${sold_at:.2f}  now=${current_mid:.2f}  "
        f"profit={profit_pct:.1%}  stock=${price:.2f}")

    if profit_pct >= EARLY_CLOSE_PCT:
        order  = buy_to_close(sym, current_mid)
        profit = (sold_at - current_mid) * 100 * CONTRACTS
        state["total_premium"] += profit
        state["current_contract"] = None
        save_state(state)
        log(f"*** 50% PROFIT — CLOSING EARLY ***  Bought back @ ${current_mid:.2f}  Locked profit: ${profit:.2f}")
        log(f"    Total premium: ${state['total_premium']:.2f}  Order: {order.get('id')}")

    return state

# ── Daily summary ─────────────────────────────────────────────────────────────
def daily_summary(state):
    price   = get_stock_price()
    account = get_account()
    pv      = float(account.get("portfolio_value", 0))
    cash    = float(account.get("cash", 0))
    print()
    print("=" * 60)
    print(f"  DAILY SUMMARY — {date.today()}  {ts()}")
    print("=" * 60)
    print(f"  Symbol           : {SYMBOL}  @ ${price:.2f}")
    print(f"  Stage            : {state['stage']}")
    print(f"  Active contract  : {state['current_contract'] or 'None'}")
    print(f"  Shares owned     : {state['shares_owned']}")
    cost = f"${state['cost_basis']:.2f}" if state['cost_basis'] else "N/A"
    print(f"  Cost basis       : {cost}")
    print(f"  Total premium    : ${state['total_premium']:.2f}")
    print(f"  Cycles complete  : {state['cycles']}")
    print(f"  Cash             : ${cash:,.2f}")
    print(f"  Portfolio value  : ${pv:,.2f}")
    total_return = pv - 100_000
    print(f"  P&L vs start     : ${total_return:+,.2f}")
    print("=" * 60)
    print()

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"  SOUN WHEEL STRATEGY  —  Paper Trading")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    state             = load_state()
    last_summary_date = None

    while True:
        try:
            if not is_market_hours():
                log("Market closed — sleeping 5 min")
                time.sleep(300)
                continue

            # daily summary near close
            if is_near_close() and last_summary_date != date.today():
                daily_summary(state)
                last_summary_date = date.today()

            # core strategy
            if state["current_contract"] is None:
                if state["stage"] == "PUT":
                    state = do_sell_put(state)
                else:
                    state = do_sell_call(state)
            else:
                state = do_monitor(state)

            time.sleep(POLL_SECS)

        except KeyboardInterrupt:
            print("\nStopped. State saved.")
            break
        except Exception as e:
            log(f"ERROR: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
