"""
Static ticker -> sector map for the Sector Map + sector filter.
Offline and free — no API. Covers the most-traded US tickers; anything
unlisted falls back to "Other".
"""

SECTOR_MAP: dict[str, str] = {
    # ── Technology ────────────────────────────────────────────────────────────
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AMD": "Technology", "INTC": "Technology", "AVGO": "Technology",
    "QCOM": "Technology", "MU": "Technology", "TXN": "Technology",
    "CRM": "Technology", "ORCL": "Technology", "IBM": "Technology",
    "ADBE": "Technology", "NOW": "Technology", "SNOW": "Technology",
    "PLTR": "Technology", "CSCO": "Technology", "DELL": "Technology",
    "HPQ": "Technology", "SMCI": "Technology", "ARM": "Technology",
    "TSM": "Technology", "ASML": "Technology", "SOUN": "Technology",
    "MSTR": "Technology", "APP": "Technology", "LCID": "Automotive",
    # ── Communication / Media ────────────────────────────────────────────────
    "GOOGL": "Communication", "GOOG": "Communication", "META": "Communication",
    "NFLX": "Communication", "DIS": "Communication", "T": "Communication",
    "VZ": "Communication", "CMCSA": "Communication", "TMUS": "Communication",
    "RDDT": "Communication", "SNAP": "Communication", "SPOT": "Communication",
    "WBD": "Communication", "PARA": "Communication", "ROKU": "Communication",
    # ── Consumer ──────────────────────────────────────────────────────────────
    "AMZN": "Consumer", "TSLA": "Automotive", "HD": "Consumer",
    "MCD": "Consumer", "NKE": "Consumer", "SBUX": "Consumer",
    "LOW": "Consumer", "TGT": "Consumer", "WMT": "Consumer",
    "COST": "Consumer", "KO": "Consumer", "PEP": "Consumer",
    "PG": "Consumer", "PM": "Consumer", "MO": "Consumer",
    "EL": "Consumer", "CMG": "Consumer", "LULU": "Consumer",
    "GME": "Consumer", "AMC": "Consumer", "JACK": "Consumer",
    "OPEN": "Real Estate", "F": "Automotive", "GM": "Automotive",
    "RIVN": "Automotive", "TM": "Automotive", "UBER": "Consumer",
    "ABNB": "Consumer", "BKNG": "Consumer", "DASH": "Consumer",
    # ── Financials ────────────────────────────────────────────────────────────
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials",
    "GS": "Financials", "MS": "Financials", "C": "Financials",
    "BLK": "Financials", "SCHW": "Financials", "AXP": "Financials",
    "V": "Financials", "MA": "Financials", "PYPL": "Financials",
    "COIN": "Financials", "HOOD": "Financials", "BRK.B": "Financials",
    "BRK-B": "Financials", "SOFI": "Financials", "LTC": "Real Estate",
    # ── Healthcare ────────────────────────────────────────────────────────────
    "JNJ": "Healthcare", "PFE": "Healthcare", "MRK": "Healthcare",
    "ABBV": "Healthcare", "LLY": "Healthcare", "UNH": "Healthcare",
    "BMY": "Healthcare", "AMGN": "Healthcare", "GILD": "Healthcare",
    "CVS": "Healthcare", "MRNA": "Healthcare", "BIIB": "Healthcare",
    "VRTX": "Healthcare", "REGN": "Healthcare", "TMO": "Healthcare",
    "ABT": "Healthcare", "DHR": "Healthcare", "ISRG": "Healthcare",
    "HIMS": "Healthcare", "VKTX": "Healthcare",
    # ── Energy ────────────────────────────────────────────────────────────────
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    "SLB": "Energy", "OXY": "Energy", "BP": "Energy",
    "SHEL": "Energy", "ET": "Energy", "KMI": "Energy",
    "FSLR": "Energy", "ENPH": "Energy", "PLUG": "Energy",
    # ── Industrials / Defense ────────────────────────────────────────────────
    "BA": "Industrials", "CAT": "Industrials", "DE": "Industrials",
    "GE": "Industrials", "HON": "Industrials", "MMM": "Industrials",
    "UPS": "Industrials", "FDX": "Industrials", "LMT": "Industrials",
    "RTX": "Industrials", "NOC": "Industrials", "GD": "Industrials",
    "UNP": "Industrials", "DAL": "Industrials", "UAL": "Industrials",
    "AAL": "Industrials", "LUV": "Industrials", "IRDM": "Industrials",
    "RKLB": "Industrials", "ACHR": "Industrials", "JOBY": "Industrials",
    # ── Materials / Real Estate / Utilities ──────────────────────────────────
    "LIN": "Materials", "FCX": "Materials", "NEM": "Materials",
    "NUE": "Materials", "DOW": "Materials",
    "PLD": "Real Estate", "AMT": "Real Estate", "SPG": "Real Estate",
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "PATH": "Technology", "DIA": "ETF", "SPY": "ETF", "QQQ": "ETF",
    "IWM": "ETF", "VOO": "ETF", "VTI": "ETF", "SOXL": "ETF",
    "TQQQ": "ETF", "TLT": "ETF", "GLD": "ETF", "USO": "ETF",
    "XLE": "ETF", "XLF": "ETF", "XLK": "ETF", "ARKK": "ETF",
    "BABA": "Consumer", "JD": "Consumer", "PDD": "Consumer",
    "NIO": "Automotive", "XPEV": "Automotive", "LI": "Automotive",
    "FUBO": "Communication", "U": "Technology", "MARA": "Financials",
    "RIOT": "Financials", "CLSK": "Financials", "WOLF": "Technology",
    "MMM": "Industrials", "WBA": "Consumer", "DIS": "Communication",
    "XOM": "Energy", "GOOGL": "Communication",
}


def sector_of(ticker: str) -> str:
    return SECTOR_MAP.get(ticker.upper().lstrip("$"), "Other")
