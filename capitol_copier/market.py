"""
Market condition checks used to gate trade execution.

  is_bull_market()    — SPY above 200-day MA; skip buys in downtrends
  has_earnings_soon() — earnings within N days; skip buys into binary events
"""

import logging
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)


def is_bull_market(index: str = "SPY", ma_days: int = 200) -> bool:
    """Return True if SPY is above its 200-day simple moving average."""
    try:
        hist = yf.Ticker(index).history(period="1y")["Close"]
        if len(hist) < ma_days:
            log.warning("Not enough history for %d-day MA — assuming bull market", ma_days)
            return True
        ma = hist.rolling(ma_days).mean().iloc[-1]
        price = hist.iloc[-1]
        bull = bool(price > ma)
        log.info(
            "Market regime: %s $%.2f vs %d-day MA $%.2f -> %s",
            index, price, ma_days, ma, "BULL" if bull else "BEAR",
        )
        return bull
    except Exception as exc:
        log.warning("Could not check market regime (%s) — assuming bull market", exc)
        return True


def has_earnings_soon(symbol: str, days: int = 7) -> bool:
    """
    Return True if the ticker is expected to report earnings within `days`
    calendar days. ETFs and tickers with no calendar return False.
    """
    try:
        cal = yf.Ticker(symbol).calendar
        if cal is None:
            return False

        # yfinance >= 0.2 returns a dict; older versions a DataFrame
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
            if not dates:
                return False
            next_dt = dates[0] if isinstance(dates, (list, tuple)) else dates
        elif hasattr(cal, "loc"):
            if "Earnings Date" not in cal.index:
                return False
            next_dt = cal.loc["Earnings Date"].iloc[0]
        else:
            return False

        if pd.isna(next_dt):
            return False

        earnings_date = pd.Timestamp(next_dt).date()
        today = datetime.now(timezone.utc).date()
        delta = (earnings_date - today).days
        if 0 <= delta <= days:
            log.info(
                "Earnings blackout: %s reports in %d day(s) (%s) — skipping buy",
                symbol, delta, earnings_date,
            )
            return True
        return False
    except Exception as exc:
        log.debug("Could not check earnings for %s: %s", symbol, exc)
        return False
