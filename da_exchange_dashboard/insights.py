"""Cross-venue insights derived from the latest ticker + depth snapshots.

These are read-only computations over data already in the SQLite store.
"""
from __future__ import annotations

import json
from collections import defaultdict


def canonical_base(venue: str, symbol: str) -> str | None:
    """Map a venue-specific THB-quoted symbol to its base asset code."""
    if not symbol:
        return None
    if venue == "binance_th" and symbol.endswith("THB"):
        return symbol[:-3]
    if venue == "upbit_th" and symbol.startswith("THB-"):
        return symbol[4:]
    # bitkub, bitazza, orbix_cg all use BASE_THB
    if symbol.endswith("_THB"):
        return symbol[:-4]
    return None


def cross_venue_summary(rows: list[dict]) -> list[dict]:
    """For every base asset present on 2+ venues, summarize price + turnover spread."""
    by_base: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        base = canonical_base(r["venue"], r["symbol"])
        if not base or r.get("last") is None:
            continue
        by_base[base].append(r)

    out = []
    for base, venue_rows in by_base.items():
        if len(venue_rows) < 2:
            continue
        lasts = {r["venue"]: r["last"] for r in venue_rows if r.get("last")}
        if len(lasts) < 2:
            continue
        mn, mx = min(lasts.values()), max(lasts.values())
        mid = (mn + mx) / 2
        spread_bps = (mx - mn) / mid * 10000 if mid else 0.0
        venues_lo = [v for v, p in lasts.items() if p == mn]
        venues_hi = [v for v, p in lasts.items() if p == mx]
        total_turnover = sum(r.get("quote_turnover_24h") or 0 for r in venue_rows)
        out.append({
            "base": base,
            "n_venues": len(lasts),
            "min_last": mn,
            "max_last": mx,
            "spread_bps": round(spread_bps, 2),
            "cheap_venue": venues_lo[0],
            "rich_venue": venues_hi[0],
            "total_turnover_24h": round(total_turnover, 2),
            "venues": {v: round(p, 6) for v, p in lasts.items()},
        })
    out.sort(key=lambda r: r["spread_bps"], reverse=True)
    return out


def top_turnover(rows: list[dict], n: int = 20) -> list[dict]:
    ranked = sorted(
        [r for r in rows if r.get("quote_turnover_24h")],
        key=lambda r: r["quote_turnover_24h"],
        reverse=True,
    )
    return ranked[:n]


def top_movers(rows: list[dict], n: int = 10) -> dict:
    valid = [r for r in rows if r.get("change_pct_24h") is not None]
    gainers = sorted(valid, key=lambda r: r["change_pct_24h"], reverse=True)[:n]
    losers = sorted(valid, key=lambda r: r["change_pct_24h"])[:n]
    return {"gainers": gainers, "losers": losers}


def venue_concentration(rows: list[dict]) -> list[dict]:
    """For each base, breakdown of cross-venue THB turnover share + HHI."""
    by_base: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        base = canonical_base(r["venue"], r["symbol"])
        turnover = r.get("quote_turnover_24h") or 0
        if base and turnover:
            by_base[base][r["venue"]] += turnover

    out = []
    for base, venues in by_base.items():
        total = sum(venues.values())
        if total <= 0 or len(venues) < 2:
            continue
        shares = {v: t / total for v, t in venues.items()}
        hhi = sum(s * s for s in shares.values())
        out.append({
            "base": base,
            "total_turnover_24h": round(total, 2),
            "n_venues": len(venues),
            "shares": {v: round(s, 4) for v, s in shares.items()},
            "hhi": round(hhi, 4),
        })
    out.sort(key=lambda r: r["total_turnover_24h"], reverse=True)
    return out


def depth_imbalance(bids_json: str, asks_json: str, mid_pct: float = 1.0) -> dict | None:
    """Top-of-book $-depth imbalance within ±mid_pct of mid price.

    imbalance = (bid_value - ask_value) / (bid_value + ask_value)
    Range [-1, 1]. Positive = bid-heavy, negative = ask-heavy.
    """
    try:
        bids = json.loads(bids_json) if isinstance(bids_json, str) else bids_json
        asks = json.loads(asks_json) if isinstance(asks_json, str) else asks_json
    except Exception:
        return None
    if not bids or not asks:
        return None
    try:
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
    except (IndexError, TypeError, ValueError):
        return None
    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return None
    floor = mid * (1 - mid_pct / 100)
    ceil = mid * (1 + mid_pct / 100)
    bid_val = sum(float(p) * float(s) for p, s in bids if floor <= float(p) <= mid)
    ask_val = sum(float(p) * float(s) for p, s in asks if mid <= float(p) <= ceil)
    total = bid_val + ask_val
    if total <= 0:
        return None
    return {
        "mid": mid,
        "bid_value_thb": round(bid_val, 2),
        "ask_value_thb": round(ask_val, 2),
        "imbalance": round((bid_val - ask_val) / total, 4),
    }
