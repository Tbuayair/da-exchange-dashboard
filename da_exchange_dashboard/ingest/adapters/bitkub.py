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


# Bitkub TradingView UDF history lives OUTSIDE /api/v3 — at /tradingview/history.
# Resolution values: "1" "5" "15" "60" "240" "1D" "1W".
_TV_BASE = "https://api.bitkub.com/tradingview"
_RESOLUTION_MAP = {
    "1m": "1", "5m": "5", "15m": "15",
    "1h": "60", "4h": "240",
    "1d": "1D", "1w": "1W",
}


def fetch_klines(symbol: str, interval: str = "1h", limit: int = 200) -> list[dict]:
    """Return canonical OHLCV bars: [{ts_ms, open, high, low, close, base_volume}, ...]."""
    import time
    res = _RESOLUTION_MAP.get(interval, "60")
    seconds_per_bar = {"1": 60, "5": 300, "15": 900, "60": 3600,
                       "240": 14400, "1D": 86400, "1W": 604800}[res]
    now = int(time.time())
    frm = now - seconds_per_bar * (limit + 2)
    r = requests.get(
        f"{_TV_BASE}/history",
        params={"symbol": symbol, "resolution": res, "from": frm, "to": now},
        timeout=15,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("s") and body["s"] != "ok":
        return []
    ts = body.get("t", []) or []
    o = body.get("o", []) or []
    h = body.get("h", []) or []
    low_arr = body.get("l", []) or []
    c = body.get("c", []) or []
    v = body.get("v", []) or []
    out = []
    for i in range(len(ts)):
        out.append({
            "ts_ms": int(ts[i]) * 1000,
            "open": _f(o[i]) if i < len(o) else None,
            "high": _f(h[i]) if i < len(h) else None,
            "low": _f(low_arr[i]) if i < len(low_arr) else None,
            "close": _f(c[i]) if i < len(c) else None,
            "base_volume": _f(v[i]) if i < len(v) else None,
        })
    return out[-limit:]


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
