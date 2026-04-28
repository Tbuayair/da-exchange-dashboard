"""CoinGecko aggregator adapter — fallback for Thai venues without a public API.

Free tier: ~30 calls/min, no auth. We hit /exchanges/{exchange_id}/tickers.

Limitations vs direct exchange feeds:
  - No raw bid/ask prices — only `bid_ask_spread_percentage`. We derive
    approximate bid/ask = last ± (last * spread_pct / 200).
  - No L2 order book depth at all.
  - 24h turnover in THB is computed as volume * last (acceptable for THB-quoted
    pairs; non-THB pairs are skipped).

Symbol format produced: `BASE_THB` (matching Bitkub for consistency).
"""
import requests

BASE = "https://api.coingecko.com/api/v3"

# venue_label -> coingecko exchange_id
EXCHANGE_IDS = {
    "bitazza": "bitazza",
    "orbix_cg": "tdax",
}


def fetch_tickers(exchange_id: str) -> list[dict]:
    """Fetch up to 100 tickers for an exchange. Single page is enough for Thai venues."""
    r = requests.get(
        f"{BASE}/exchanges/{exchange_id}/tickers",
        params={"page": 1},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("tickers", [])


def normalize_thb_tickers(raw_tickers: list[dict], venue_label: str) -> list[dict]:
    """Filter to THB-quoted pairs and project to canonical schema."""
    out = []
    for t in raw_tickers:
        if t.get("target") != "THB":
            continue
        base = t.get("base")
        last = _f(t.get("last"))
        volume = _f(t.get("volume"))
        spread_pct = _f(t.get("bid_ask_spread_percentage"))
        bid = ask = None
        if last is not None and spread_pct is not None:
            half = last * spread_pct / 200.0
            bid = last - half
            ask = last + half
        turnover = (volume * last) if (volume is not None and last is not None) else None
        out.append({
            "venue": venue_label,
            "symbol": f"{base}_THB" if base else None,
            "last": last,
            "bid": bid,
            "ask": ask,
            "high_24h": None,
            "low_24h": None,
            "base_volume_24h": volume,
            "quote_turnover_24h": turnover,
            "change_pct_24h": None,
        })
    return out


def fetch_volume_chart(exchange_id: str, days: int = 14) -> list[dict]:
    """Daily volume series for an exchange. Free tier supports up to 31 days.

    Response: [[ts_ms, volume_btc_string], ...].
    Returns canonical [{ts_ms, volume_btc}, ...].
    """
    r = requests.get(
        f"{BASE}/exchanges/{exchange_id}/volume_chart",
        params={"days": days},
        timeout=15,
    )
    r.raise_for_status()
    out = []
    for ts, v in r.json():
        out.append({"ts_ms": int(ts), "volume_btc": _f(v)})
    return out


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
