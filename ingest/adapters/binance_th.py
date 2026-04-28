"""Binance TH public market-data adapter — REST v1, no auth required.

Quirks vs global Binance:
  - Base path is /api/v1 (v3 returns 404).
  - /ticker/24hr requires a `symbol` parameter — does NOT support fetch-all.
  - /ticker/bookTicker and /ticker/price both support fetch-all.
  - Use /exchangeInfo to discover the THB-quoted pairs (~11 of them today),
    then fetch per-symbol 24h stats — well within the 6000/min weight budget.

Symbol format is concatenated, e.g. 'BTCTHB'.
"""
import requests

BASE = "https://api.binance.th/api/v1"
VENUE = "binance_th"


def fetch_thb_symbols() -> list[str]:
    """Discover all THB-quoted symbols currently trading."""
    r = requests.get(f"{BASE}/exchangeInfo", timeout=10)
    r.raise_for_status()
    data = r.json()
    return [
        s["symbol"]
        for s in data.get("symbols", [])
        if s.get("quoteAsset") == "THB" and s.get("status") == "TRADING"
    ]


def fetch_book_tickers() -> list[dict]:
    """Best bid/ask for ALL symbols (L1 snapshot, single call)."""
    r = requests.get(f"{BASE}/ticker/bookTicker", timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_24h(symbol: str) -> dict:
    """24h rolling stats for one symbol (required because /24hr has no fetch-all)."""
    r = requests.get(f"{BASE}/ticker/24hr", params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    return r.json()


def normalize_ticker(t24h: dict, book: dict | None = None) -> dict:
    return {
        "venue": VENUE,
        "symbol": t24h.get("symbol"),
        "last": _f(t24h.get("lastPrice")),
        "bid": _f((book or {}).get("bidPrice")) or _f(t24h.get("bidPrice")),
        "ask": _f((book or {}).get("askPrice")) or _f(t24h.get("askPrice")),
        "high_24h": _f(t24h.get("highPrice")),
        "low_24h": _f(t24h.get("lowPrice")),
        "base_volume_24h": _f(t24h.get("volume")),
        "quote_turnover_24h": _f(t24h.get("quoteVolume")),
        "change_pct_24h": _f(t24h.get("priceChangePercent")),
    }


def fetch_depth(symbol: str, limit: int = 20) -> dict:
    """L2 order book. symbol e.g. 'BTCTHB'. Allowed limits: 5,10,20,50,100,500,1000."""
    r = requests.get(
        f"{BASE}/depth",
        params={"symbol": symbol, "limit": limit},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def _f(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f != 0 else None
    except (TypeError, ValueError):
        return None
