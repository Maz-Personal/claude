"""
Sector ticker classification for per-politician trade filters.
Each set covers the S&P 500 constituents for that sector plus
ETFs and the specific names each politician is known to trade.
"""

# ── Energy ────────────────────────────────────────────────────────────────────
ENERGY_TICKERS: set[str] = {
    # Integrated / Major
    "XOM", "CVX", "COP", "OXY", "MPC", "PSX", "VLO", "HES",
    # E&P
    "DVN", "PXD", "EOG", "FANG", "APA", "MRO", "AR", "EQT",
    "CTRA", "SWN", "RRC", "CNX", "SM", "MTDR", "CHRD", "NOG", "PR",
    # Midstream / Pipeline
    "OKE", "WMB", "KMI", "ET", "EPD", "MPLX", "PAA", "PAGP", "MMP", "LNG",
    # Services & Equipment
    "SLB", "HAL", "BKR", "NOV", "NE", "RIG", "DO", "HP", "PTEN",
    # Power / Utilities (energy-adjacent)
    "VST", "CEG", "ETR", "AES", "NEE",
    # ETFs
    "XLE", "VDE", "OIH", "XOP", "IEO", "AMLP", "BNO", "USO", "UNG",
}

# ── Technology (broad) ────────────────────────────────────────────────────────
TECH_TICKERS: set[str] = {
    # Mega-cap
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "META", "AMZN", "TSLA",
    # Semiconductors (see also SEMIS below)
    "QCOM", "TXN", "AVGO", "AMD", "INTC", "MU", "AMAT", "LRCX", "KLAC",
    "MRVL", "MPWR", "ON", "SWKS", "ADI", "MCHP",
    # Software / Cloud / Cybersecurity
    "CRM", "ORCL", "SAP", "NOW", "SNOW", "PANW", "CRWD", "ZS", "FTNT",
    "INTU", "ADBE", "WDAY", "TEAM", "DDOG", "MDB", "PLTR",
    # Hardware / Infra
    "CSCO", "IBM", "HPE", "DELL", "HPQ", "WDC", "STX",
    # IT Services
    "ACN", "CTSH", "IT", "INFY", "EPAM",
    # ETFs
    "QQQ", "XLK", "VGT", "IGV", "CIBR",
}

# ── Semiconductors (Meuser focus — CHIPS Act / Science Committee) ─────────────
SEMIS_TICKERS: set[str] = {
    "NVDA", "AVGO", "AMD", "INTC", "QCOM", "TXN", "MU", "AMAT",
    "LRCX", "KLAC", "MRVL", "MPWR", "ON", "SWKS", "QRVO", "ADI",
    "MCHP", "WOLF", "CRUS", "SLAB", "NXPI", "STM", "TSM", "ASML",
    "SOXX", "SMH",  # ETFs
}

# ── Financials (French Hill — House Financial Services Chair) ─────────────────
FINANCIALS_TICKERS: set[str] = {
    # Banks
    "JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "TFC", "PNC",
    "COF", "AXP", "DFS", "SYF", "ALLY",
    # Regional banks
    "FITB", "HBAN", "RF", "KEY", "CFG", "ZION", "WAL", "PACW",
    # Insurance
    "BRK-B", "BRK.B", "MET", "PRU", "AIG", "PGR", "TRV", "CB", "AIG",
    "ACGL", "RNR", "MKL",
    # Asset Management / Brokerage
    "BLK", "SCHW", "BEN", "IVZ", "AMG", "APO", "KKR", "BX", "CG",
    "ARES", "AB",
    # Payments / Fintech
    "V", "MA", "PYPL", "SQ", "FIS", "FISV", "GPN", "WEX", "JNPR",
    # ETFs
    "XLF", "VFH", "KBE", "KRE", "IAI",
}

# ── Healthcare / Pharma (Burgess — physician, Energy & Commerce Health) ───────
HEALTHCARE_TICKERS: set[str] = {
    # Large-cap pharma
    "JNJ", "PFE", "MRK", "ABBV", "LLY", "BMY", "AMGN", "GILD",
    "BIIB", "REGN", "VRTX", "MRNA", "BNTX",
    # Med devices
    "ABT", "MDT", "SYK", "BSX", "EW", "ISRG", "ZBH", "BAX", "BDX",
    # Genomics / Biotech (Burgess's Illumina trades)
    "ILMN", "PACB", "NVAX", "CRSP", "EDIT", "BEAM", "NTLA",
    # Healthcare services / insurance
    "UNH", "CVS", "CI", "HUM", "ELV", "CNC", "MOH", "HCA",
    "THC", "UHS", "ENSG",
    # CRO / Life sciences tools
    "TMO", "DHR", "IQV", "MEDP", "ICLR", "A", "PKI", "NTRA",
    # ETFs
    "XLV", "IBB", "VHT", "IHI", "XBI",
}

# ── Defense / Industrials (Scott Franklin — Industrials #2 sector) ────────────
DEFENSE_TICKERS: set[str] = {
    # Prime defense contractors
    "LMT", "RTX", "NOC", "GD", "BA", "L3H", "LHX", "HII",
    # Defense IT / services
    "LDOS", "SAIC", "BAH", "CACI", "KEYW", "MANT", "DXC",
    # Defense electronics / missiles
    "TDG", "HEI", "TXT", "KTOS", "AJRD", "RCAT",
    # Space / satellite
    "SPCE", "RKLB", "ASTS", "LUNR", "PL",
    # Broader industrials Franklin trades (UPS, industrial conglomerates)
    "UPS", "FDX", "GE", "HON", "MMM", "CAT", "DE", "ITW", "EMR",
    "ROK", "PH", "AME", "XYL", "CARR", "TT", "IR",
    # ETFs
    "XAR", "ITA", "PPA", "DFEN",
}


def is_energy(ticker: str) -> bool:
    return ticker.upper() in ENERGY_TICKERS

def is_tech(ticker: str) -> bool:
    return ticker.upper() in TECH_TICKERS

def is_semis(ticker: str) -> bool:
    return ticker.upper() in SEMIS_TICKERS

def is_financials(ticker: str) -> bool:
    t = ticker.upper().replace("-", ".") # handle BRK-B vs BRK.B
    return t in FINANCIALS_TICKERS or ticker.upper() in FINANCIALS_TICKERS

def is_healthcare(ticker: str) -> bool:
    return ticker.upper() in HEALTHCARE_TICKERS

def is_defense(ticker: str) -> bool:
    return ticker.upper() in DEFENSE_TICKERS
