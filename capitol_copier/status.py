"""Quick status dashboard — run any time to see bot health."""
import json
from collections import defaultdict
from pathlib import Path
from trader import get_account, get_client
from config import TARGETS

state_path = Path(__file__).parent / "state.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {}

print("=" * 60)
print("  Capitol Copier — Status")
print("=" * 60)

acct = get_account()
print(f"  Alpaca cash:     ${float(acct.cash):>12,.2f}")
print(f"  Alpaca equity:   ${float(acct.equity):>12,.2f}")
print(f"  Account status:  {acct.status}")
print()

positions = get_client().get_all_positions()
if positions:
    print(f"  Open positions ({len(positions)}):")
    total_pnl = 0.0
    for p in positions:
        pnl = float(p.unrealized_pl)
        total_pnl += pnl
        sign = "+" if pnl >= 0 else ""
        print(f"    {p.symbol:6s}  {float(p.qty):>8.4f} sh  "
              f"${float(p.market_value):>10,.2f}  P&L {sign}${pnl:,.2f}")
    sign = "+" if total_pnl >= 0 else ""
    print(f"    {'TOTAL':6s}  {'':>17s}  {'':>12s}  P&L {sign}${total_pnl:,.2f}")
else:
    print("  No open positions")

print()
print(f"  Last run:  {state.get('last_run', 'never')}")
print(f"  Trades seen:    {len(state.get('seen_trade_ids', []))}")

executed = state.get("executed", [])
print(f"  Trades copied:  {len(executed)}")

if executed:
    by_pol = defaultdict(list)
    for t in executed:
        by_pol[t.get("politician", "unknown")].append(t)

    print()
    for target in TARGETS:
        name = target["name"]
        sf   = target.get("sector_filter") or "all sectors"
        pol_trades = by_pol.get(name, [])
        print(f"  {name} ({sf}): {len(pol_trades)} trade(s) copied")
        for t in pol_trades[-5:]:
            print(f"    {t.get('copied_at','')[:19]}  {t.get('side','').upper():4s}  "
                  f"{t.get('symbol',''):6s}  order={t.get('id','')}")

print("=" * 60)
