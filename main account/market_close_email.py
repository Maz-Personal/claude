"""
Market Close Summary Email — V18.9 Agent
Runs at 4:05 PM ET weekdays via cron.
Sends a summary of v18_agent state, PnL, and positions to maz.zabaneh@gmail.com
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
import sendgrid
from sendgrid.helpers.mail import Mail

_DIR = Path(__file__).parent
load_dotenv(_DIR.parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
SENDGRID_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL   = "maz.zabaneh@gmail.com"
TO_EMAIL     = "maz.zabaneh@gmail.com"
LEDGER_FILE  = _DIR / "v18_shadow_ledger.json"
TS_LEDGER    = _DIR / "nvda_trailing_state.json"

# Alpaca
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.requests import GetOptionContractsRequest

API_KEY    = os.getenv("WHEEL_ALPACA_API_KEY")
API_SECRET = os.getenv("WHEEL_ALPACA_API_SECRET")
trading    = TradingClient(API_KEY, API_SECRET, paper=True)
data       = StockHistoricalDataClient(API_KEY, API_SECRET)


def load_ledger(path):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def get_positions():
    try:
        return {p.symbol: p for p in trading.get_all_positions()}
    except Exception:
        return {}


def get_account():
    try:
        return trading.get_account()
    except Exception:
        return None


def get_price(ticker):
    try:
        resp = data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
        return float(resp[ticker].price)
    except Exception:
        return None


def get_option_price(symbol):
    try:
        underlying = ''.join(c for c in symbol if c.isalpha())[:4].rstrip('CP')
        contracts = trading.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[underlying],
        ))
        match = next((c for c in contracts.option_contracts if c.symbol == symbol), None)
        if match and match.close_price:
            return float(match.close_price)
    except Exception:
        pass
    return None


def build_email():
    now_et = datetime.now(timezone.utc).strftime("%Y-%m-%d %I:%M %p UTC")
    ledger  = load_ledger(LEDGER_FILE)
    acct    = get_account()
    positions = get_positions()

    agent_state = ledger.get("agent_state", "UNKNOWN")
    orders      = ledger.get("orders", {})
    sessions    = ledger.get("sessions", {})
    last_sync   = ledger.get("last_sync", "N/A")

    # Account summary
    equity      = f"${float(acct.equity):,.2f}"    if acct else "N/A"
    cash        = f"${float(acct.cash):,.2f}"      if acct else "N/A"
    buying_pw   = f"${float(acct.buying_power):,.2f}" if acct else "N/A"

    # Underlying prices
    nvda_price = get_price("NVDA")
    xle_price  = get_price("XLE")

    # Build legs table
    leg_rows = ""
    if orders:
        for leg_name, order in orders.items():
            sym   = order.get("symbol", "—")
            side  = order.get("side", "—").upper()
            qty   = order.get("qty", "—")
            price = get_option_price(sym)
            price_str = f"${price:.2f}" if price else "N/A"
            leg_rows += f"""
            <tr>
              <td style="padding:6px 12px;border-bottom:1px solid #eee">{leg_name}</td>
              <td style="padding:6px 12px;border-bottom:1px solid #eee">{sym}</td>
              <td style="padding:6px 12px;border-bottom:1px solid #eee">{side}</td>
              <td style="padding:6px 12px;border-bottom:1px solid #eee">{qty}</td>
              <td style="padding:6px 12px;border-bottom:1px solid #eee">{price_str}</td>
            </tr>"""
    else:
        leg_rows = '<tr><td colspan="5" style="padding:6px 12px;color:#888">No legs recorded</td></tr>'

    # State color
    state_color = {
        "PENDING":    "#f0ad4e",
        "OPEN":       "#5cb85c",
        "SANDBOX":    "#5bc0de",
        "LIQUIDATED": "#d9534f",
    }.get(agent_state.split("_")[0], "#999")

    # Trailing stop summary
    ts_ledger   = load_ledger(TS_LEDGER)
    ts_symbol   = ts_ledger.get("symbol", "NVDA")
    ts_entry    = ts_ledger.get("entry_price", "—")
    ts_stop     = ts_ledger.get("stop_loss", "—")
    ts_qty      = ts_ledger.get("total_qty", "—")
    ts_closed   = ts_ledger.get("position_closed", False)
    ts_status   = "CLOSED" if ts_closed else "ACTIVE"
    ts_color    = "#d9534f" if ts_closed else "#5cb85c"

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:640px;margin:auto;color:#333">

    <div style="background:#1a1a2e;padding:20px;border-radius:8px 8px 0 0">
      <h2 style="color:#fff;margin:0">📊 Trading Bot — Market Close Summary</h2>
      <p style="color:#aaa;margin:4px 0 0">{now_et}</p>
    </div>

    <!-- Account -->
    <div style="background:#f8f9fa;padding:16px;border:1px solid #ddd;border-top:none">
      <h3 style="margin:0 0 10px">💼 Wheel Account</h3>
      <table style="width:100%">
        <tr><td><b>Equity</b></td><td>{equity}</td>
            <td><b>Cash</b></td><td>{cash}</td>
            <td><b>Buying Power</b></td><td>{buying_pw}</td></tr>
      </table>
    </div>

    <!-- Underlying -->
    <div style="background:#fff;padding:16px;border:1px solid #ddd;border-top:none">
      <h3 style="margin:0 0 10px">📈 Underlying Prices</h3>
      <table style="width:100%">
        <tr>
          <td><b>NVDA</b></td><td>${f"{nvda_price:.2f}" if nvda_price else "N/A"}</td>
          <td><b>XLE</b></td><td>${f"{xle_price:.2f}" if xle_price else "N/A"}</td>
        </tr>
      </table>
    </div>

    <!-- V18 Agent -->
    <div style="background:#f8f9fa;padding:16px;border:1px solid #ddd;border-top:none">
      <h3 style="margin:0 0 10px">🤖 V18.9 Options Agent</h3>
      <p><b>State:</b> <span style="background:{state_color};color:#fff;padding:2px 10px;border-radius:12px;font-size:13px">{agent_state}</span></p>
      <p style="color:#888;font-size:12px">Last ledger sync: {last_sync}</p>
      <table style="width:100%;border-collapse:collapse;margin-top:8px">
        <tr style="background:#eee">
          <th style="padding:6px 12px;text-align:left">Leg</th>
          <th style="padding:6px 12px;text-align:left">Symbol</th>
          <th style="padding:6px 12px;text-align:left">Side</th>
          <th style="padding:6px 12px;text-align:left">Qty</th>
          <th style="padding:6px 12px;text-align:left">Last Price</th>
        </tr>
        {leg_rows}
      </table>
    </div>

    <!-- Trailing Stop -->
    <div style="background:#fff;padding:16px;border:1px solid #ddd;border-top:none">
      <h3 style="margin:0 0 10px">🛑 Trailing Stop — {ts_symbol}</h3>
      <p><b>Status:</b> <span style="background:{ts_color};color:#fff;padding:2px 10px;border-radius:12px;font-size:13px">{ts_status}</span></p>
      <table style="width:100%">
        <tr>
          <td><b>Entry</b></td><td>${ts_entry}</td>
          <td><b>Stop</b></td><td>${ts_stop}</td>
          <td><b>Qty</b></td><td>{ts_qty}</td>
        </tr>
      </table>
    </div>

    <div style="background:#1a1a2e;padding:12px 20px;border-radius:0 0 8px 8px;text-align:center">
      <p style="color:#aaa;font-size:12px;margin:0">Trading Bot — Paper Account | Auto-generated at market close</p>
    </div>

    </body></html>
    """
    return html


def send_email(html_body):
    subject = f"📊 Market Close Summary — {datetime.now().strftime('%b %d, %Y')}"
    message = Mail(
        from_email=FROM_EMAIL,
        to_emails=TO_EMAIL,
        subject=subject,
        html_content=html_body,
    )
    sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_KEY)
    response = sg.send(message)
    print(f"Email sent to {TO_EMAIL} — status {response.status_code}")


if __name__ == "__main__":
    html = build_email()
    send_email(html)
    print("Done.")
