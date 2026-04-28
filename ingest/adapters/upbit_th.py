"""Upbit TH public market-data adapter — REST v1, no auth required.

Verified live 2026-04-28: th-api.upbit.com/v1.

Quirks:
  - Symbol format is QUOTE-BASE, e.g. 'THB-BTC' (note the dash, opposite of Bitkub).
  - /v1/ticker has no top-level bid/ask — fetch /v1/orderbook for L1.
  - 24h turnover lives in `acc_trade_price_24h`.
  - change_rate is a decimal (0.0358 = 3.58%).
"""
import requests

BASE = "https://th-api.upbit.com/v1"
VENUE = "upbit_th"


def fetch_thb_markets() -> list[str]:
    """All THB-quoted markets currently listed."""
    r = requests.get(f"{BASE}/market/all", timeout=10)
    r.raise_for_status()
    return [m["market"] for m in r.json() if m["market"].startswith("THB-")]


def fetch_tickers(markets: list[str]) -> list[dict]:
    """Batch ticker fetch — Upbit accepts comma-separated markets."""
    if not markets:
        return []
    r = requests.get(
        f"{BASE}/ticker",
        params={"markets": ",".join(markets)},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def fetch_orderbooks(markets: list[str]) -> list[dict]:
    """Batch orderbook fetch — comma-separated markets, full depth (~15 levels)."""
    if not markets:
        return []
    r = requests.get(
        f"{BASE}/orderbook",
        params={"markets": ",".join(markets)},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def normalize_ticker(t: dict, ob: dict | None = None) -> dict:
    """Combine ticker + (optional) top-of-book from orderbook."""
    bid = ask = None
    if ob and ob.get("orderbook_units"):
        top = ob["orderbook_units"][0]
        bid = _f(top.get("bid_price"))
        ask = _f(top.get("ask_price"))
    change_rate = _f(t.get("change_rate"))
    sign = 1 if t.get("change") == "RISE" else (-1 if t.get("change") == "FALL" else 0)
    change_pct = change_rate * 100 * sign if change_rate is not None else None
    return {
        "venue": VENUE,
        "symbol": t.get("market"),
        "last": _f(t.get("trade_price")),
        "bid": bid,
        "ask": ask,
        "high_24h": _f(t.get("high_price")),
        "low_24h": _f(t.get("low_price")),
        "base_volume_24h": _f(t.get("acc_trade_volume_24h")),
        "quote_turnover_24h": _f(t.get("acc_trade_price_24h")),
        "change_pct_24h": change_pct,
    }


def normalize_depth(ob: dict, depth: int = 20) -> dict:
    """Convert Upbit orderbook to canonical {bids, asks} of [[price, size], ...]."""
    units = ob.get("orderbook_units", [])[:depth]
    return {
        "bids": [[_f(u.get("bid_price")), _f(u.get("bid_size"))] for u in units],
        "asks": [[_f(u.get("ask_price")), _f(u.get("ask_size"))] for u in units],
    }


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
