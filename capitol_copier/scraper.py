"""
Scrapes Capitol Trades for a politician's most recent trades.
Parses the server-rendered HTML table — column layout confirmed from live HTML.
"""

import json
import re
import logging
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

from config import CAPITOL_TRADES_BASE

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.capitoltrades.com/trades",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def _parse_amount(amount_str: str) -> float:
    """Return the midpoint of a range like '500K–1M' as a float."""
    clean = amount_str.replace("$", "").replace(",", "").strip()
    clean = re.sub(r"[^\w.\-]", "-", clean)
    parts = re.split(r"-+", clean)
    values = []
    for p in parts:
        p = p.strip().upper()
        if not p:
            continue
        if p.endswith("M"):
            try:
                values.append(float(p[:-1]) * 1_000_000)
            except ValueError:
                pass
        elif p.endswith("K"):
            try:
                values.append(float(p[:-1]) * 1_000)
            except ValueError:
                pass
        else:
            try:
                values.append(float(p))
            except ValueError:
                pass
    return sum(values) / len(values) if values else 0.0


def _normalise_action(raw: str) -> str:
    raw = raw.lower().strip()
    if "buy" in raw or "purchase" in raw:
        return "buy"
    if "sell" in raw or "sale" in raw:
        return "sell"
    return raw


_MONTH_FIX = {"Sept": "Sep", "June": "Jun", "July": "Jul"}


def _parse_date_cell(cell) -> str:
    """Parse a two-div date cell like '7 Jun' / '2023' into 'YYYY-MM-DD'."""
    container = cell.select_one(".text-center")
    if container:
        divs = container.find_all("div", recursive=False)
        parts = [d.get_text(strip=True) for d in divs if d.get_text(strip=True)]
    else:
        divs = [d for d in cell.find_all("div") if not d.find("div")]
        parts = [d.get_text(strip=True) for d in divs if d.get_text(strip=True)]
    text = " ".join(parts[:2])
    for wrong, right in _MONTH_FIX.items():
        text = text.replace(wrong, right)
    try:
        return datetime.strptime(text, "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return text


def _parse_row(row) -> dict | None:
    cells = row.find_all("td")
    if len(cells) < 9:
        return None

    # [1] Issuer: ticker from span.issuer-ticker
    ticker_tag = cells[1].select_one(".issuer-ticker")
    if not ticker_tag:
        return None
    ticker_raw = ticker_tag.get_text(strip=True)
    ticker = ticker_raw.split(":")[0].upper()

    company_tag = cells[1].select_one(".issuer-name")
    company = company_tag.get_text(strip=True) if company_tag else ticker

    # [2] Published date
    pub_date = _parse_date_cell(cells[2])

    # [3] Traded date
    trade_date = _parse_date_cell(cells[3])

    # [6] Transaction type
    type_tag = cells[6].select_one(".tx-type")
    if not type_tag:
        return None
    action = _normalise_action(type_tag.get_text(strip=True))

    # [7] Trade size
    size_text_tag = cells[7].select_one("span.mt-1") or cells[7].select_one(".text-txt-dimmer")
    amount_str = size_text_tag.get_text(strip=True) if size_text_tag else ""
    amount = _parse_amount(amount_str)

    # [9] Trade detail link → unique ID
    link_tag = cells[9].select_one("a[href*='/trades/']") if len(cells) > 9 else None
    trade_id = ""
    if link_tag:
        href = link_tag.get("href", "")
        m = re.search(r"/trades/(\d+)", href)
        if m:
            trade_id = m.group(1)

    if not ticker or ticker in {"N/A", "NA", "-"}:
        return None

    if not trade_id:
        trade_id = f"{ticker}_{pub_date}_{action}"

    company_lower = company.lower()
    is_option = any(kw in company_lower for kw in ("option", " call", " put"))
    asset_type = "option" if is_option else "Stock"

    return {
        "_id":        trade_id,   # ✅ FIX: set _id here so it's always present
        "ticker":     ticker,
        "company":    company,
        "action":     action,
        "amount":     amount,
        "trade_date": trade_date,
        "pub_date":   pub_date,
        "asset_type": asset_type,
        "trade_id":   trade_id,
    }


def get_recent_trades(politician_id: str, pages: int = 3) -> list[dict]:
    """Fetch the N most recent pages of trades for a politician. Returns newest first."""
    all_trades: list[dict] = []

    for page in range(1, pages + 1):
        url = (
            f"{CAPITOL_TRADES_BASE}/trades"
            f"?politician={politician_id}"
            f"&sortBy=pubDate&sortDir=desc"
            f"&pageSize=20&page={page}"
        )
        try:
            resp = SESSION.get(url, timeout=25)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Capitol Trades fetch error (page %d): %s", page, exc)
            break

        soup = BeautifulSoup(resp.text, "lxml")
        rows = soup.select("table tbody tr")
        if not rows:
            log.info("No rows on page %d, stopping", page)
            break

        page_trades = []
        for row in rows:
            trade = _parse_row(row)
            if trade:
                page_trades.append(trade)

        log.info("Page %d: parsed %d/%d rows", page, len(page_trades), len(rows))
        all_trades.extend(page_trades)

    log.info("Total trades fetched for %s: %d", politician_id, len(all_trades))
    return all_trades


def filter_new_trades(trades: list[dict], seen_ids: set[str], max_age_days: int) -> list[dict]:
    """Return only trades that are new (unseen) and within max_age_days of publication."""
    now = datetime.now(timezone.utc)
    result = []
    for t in trades:
        trade_id = t.get("_id", "")   # ✅ FIX: _id is now always set by _parse_row
        if not trade_id or trade_id in seen_ids:
            continue
        pub_date_str = t.get("pub_date", "")
        if pub_date_str:
            try:
                pub_dt = datetime.strptime(pub_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age = (now - pub_dt).days
                if age > max_age_days:
                    log.debug("Skipping stale trade %s (%d days old)", trade_id, age)
                    continue
            except ValueError:
                pass
        result.append(t)
    return result
