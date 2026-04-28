"""Bitkub public market-data adapter — REST v3, no auth required.

Verified field names (snake_case): symbol, last, lowest_ask, highest_bid,
base_volume, quote_volume, high_24_hr, low_24_hr, percent_change.
Symbol format is BASE_QUOTE, e.g. 'BTC_THB'.
"""
import requests

BASE = "https://api.bitkub.com/api/v3"
VENUE = "bitkub"


def fetch_tickers() -> list[dict]:
    """All market tickers in one call."""
    r = requests.get(f"{BASE}/market/ticker", timeout=10)
    r.raise_for_status()
    return r.json()


def normalize_ticker(raw: dict) -> dict:
    return {
        "venue": VENUE,
        "symbol": raw.get("symbol"),
        "last": _f(raw.get("last")),
        "bid": _f(raw.get("highest_bid")),
        "ask": _f(raw.get("lowest_ask")),
        "high_24h": _f(raw.get("high_24_hr")),
        "low_24h": _f(raw.get("low_24_hr")),
        "base_volume_24h": _f(raw.get("base_volume")),
        "quote_turnover_24h": _f(raw.get("quote_volume")),
        "change_pct_24h": _f(raw.get("percent_change")),
    }


def fetch_depth(symbol: str, limit: int = 20) -> dict:
    """L2 order book for one symbol. symbol e.g. 'BTC_THB'.

    Response is wrapped: {"error": 0, "result": {"bids": [...], "asks": [...]}}.
    Returns the unwrapped {bids, asks} dict.
    """
    r = requests.get(
        f"{BASE}/market/depth",
        params={"sym": symbol, "lmt": limit},
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("error") not in (0, None):
        raise RuntimeError(f"bitkub depth error code={body.get('error')} for {symbol}")
    return body.get("result") or {"bids": [], "asks": []}


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
