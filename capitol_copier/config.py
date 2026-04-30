import os
from dotenv import dotenv_values

# Load from .env in the project root (one level up from capitol_copier/)
_env = dotenv_values(os.path.join(os.path.dirname(__file__), "..", ".env"))

ALPACA_API_KEY    = _env.get("CAPITOL_ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = _env.get("CAPITOL_ALPACA_API_SECRET", "")
ALPACA_BASE_URL   = "https://paper-api.alpaca.markets"

# Capitol Trades
CAPITOL_TRADES_BASE = "https://www.capitoltrades.com"

# 15% per trade — politicians file rarely so each signal deserves real size
TRADE_SIZE_PCT = 0.15

# Reserve this % of total equity as minimum cash buffer (never deploy below this)
CASH_RESERVE_PCT = 0.10

# Park idle cash in SPY between politician trades
BASE_HOLDING = "SPY"
# Target allocation: keep this fraction of portfolio in SPY when no trades pending
BASE_HOLDING_PCT = 0.70

# Only execute trades published within this many days (max congressional filing delay)
MAX_TRADE_AGE_DAYS = 45

STATE_FILE = "state.json"
LOG_FILE   = "bot.log"

# Trailing stop: sell if price drops this % from its tracked peak
TRAILING_STOP_PCT = 0.10

# Single-position cap: never hold more than this fraction of equity in one ticker
POSITION_CAP_PCT = 0.20

TARGETS = [
    {
        "id":            "P000197",
        "name":          "Nancy Pelosi",
        "sector_filter": None,           # all — high conviction large-cap tech bets
    },
    {
        "id":            "H001082",
        "name":          "Kevin Hern",
        "sector_filter": "energy",       # Oklahoma/energy committee, DVN/OKE/WMB
    },
    {
        "id":            "M001204",
        "name":          "Dan Meuser",
        "sector_filter": "semis",        # 15/27 trades = NVDA, House Science/CHIPS Act
    },
    {
        "id":            "H001072",
        "name":          "French Hill",
        "sector_filter": "financials",   # Chair, House Financial Services Committee
    },
    {
        "id":            "B001248",
        "name":          "Michael Burgess",
        "sector_filter": "healthcare",   # Physician, Energy & Commerce Health Subcommittee
    },
    {
        "id":            "F000472",
        "name":          "Scott Franklin",
        "sector_filter": "defense",      # Best available defense/industrials signal
    },
]
