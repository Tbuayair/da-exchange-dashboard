"""InnovestX (INVX) digital-asset adapter — authenticated REST.

Auth scheme (per https://api-docs.innovestxonline.com): every request must carry
four headers — X-INVX-APIKEY, X-INVX-SIGNATURE (HmacSHA256), X-INVX-TIMESTAMP
(ms epoch), and X-INVX-REQUEST-UID (uuid4). Timestamps must be within 150s of
server time.

The ticker endpoint returns ONE-MINUTE OHLCV + best bid/ask, not 24h-rolling
figures. We therefore leave 24h fields (high/low/volume/turnover/change_pct) as
None on first integration; a follow-up can compute them locally from polled
history.

Symbols on InnovestX have no separator: 'BTCTHB' (vs Bitkub 'BTC_THB').
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone

import requests

BASE = "https://api.innovestxonline.com"
HOST = "api.innovestxonline.com"
PATH_PREFIX = "/api/v1/digital-asset"
VENUE = "innovestx"
QUOTE = "THB"


class InnovestXAuthError(RuntimeError):
    """Raised when API credentials are missing from the environment."""


def _credentials() -> tuple[str, str]:
    key = os.environ.get("INVX_API_KEY")
    secret = os.environ.get("INVX_API_SECRET")
    if not key or not secret:
        raise InnovestXAuthError("INVX_API_KEY and INVX_API_SECRET must be set")
    return key, secret


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def build_signature(
    api_key: str,
    api_secret: str,
    method: str,
    path: str,
    body_str: str,
    request_uid: str,
    timestamp_ms: int,
    query: str = "",
    content_type: str = "application/json",
) -> str:
    """HmacSHA256(api_secret, apikey+method+host+path+query+content-type+uid+ts+body)."""
    content_to_sign = (
        api_key
        + method.upper()
        + HOST
        + path
        + query
        + content_type
        + request_uid
        + str(timestamp_ms)
        + body_str
    )
    return hmac.new(
        api_secret.encode("utf-8"),
        content_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _request(method: str, path: str, body: dict | None = None, timeout: int = 10) -> dict:
    api_key, api_secret = _credentials()
    body_str = json.dumps(body) if body is not None else ""
    request_uid = str(uuid.uuid4())
    ts = _now_ms()
    sig = build_signature(api_key, api_secret, method, path, body_str, request_uid, ts)
    headers = {
        "Content-Type": "application/json",
        "X-INVX-APIKEY": api_key,
        "X-INVX-SIGNATURE": sig,
        "X-INVX-TIMESTAMP": str(ts),
        "X-INVX-REQUEST-UID": request_uid,
    }
    url = BASE + path
    if method.upper() == "GET":
        r = requests.get(url, headers=headers, timeout=timeout)
    else:
        r = requests.request(method, url, headers=headers, data=body_str, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    code = payload.get("code") or payload.get("status")
    if code not in (None, "0000"):
        raise RuntimeError(f"innovestx {path} error code={code} message={payload.get('message')}")
    return payload


def fetch_symbols() -> list[dict]:
    """GET /symbols — returns full symbol catalogue."""
    payload = _request("GET", f"{PATH_PREFIX}/symbols")
    return payload.get("data") or []


def fetch_ticker(symbol: str) -> dict | None:
    """POST /ticker/subscribe — latest 1-minute OHLCV bar + best bid/ask for one symbol.

    Returns the latest bar dict, or None if data is empty.
    """
    payload = _request("POST", f"{PATH_PREFIX}/ticker/subscribe", body={"symbol": symbol})
    rows = payload.get("data") or []
    return rows[-1] if rows else None


def fetch_depth(symbol: str, depth: int = 100) -> dict:
    """POST /orderbook/lvl2 — Level-2 order book.

    Returns standard {bids, asks} dict with each side sorted best-first.
    """
    payload = _request("POST", f"{PATH_PREFIX}/orderbook/lvl2", body={"symbol": symbol, "depth": depth})
    rows = payload.get("data") or []
    bids: list[list[float]] = []
    asks: list[list[float]] = []
    for r in rows:
        try:
            price = float(r.get("price"))
            qty = float(r.get("quantity"))
        except (TypeError, ValueError):
            continue
        side = r.get("side")  # 0 buy, 1 sell
        (bids if side == 0 else asks).append([price, qty])
    bids.sort(key=lambda x: -x[0])  # highest bid first
    asks.sort(key=lambda x: x[0])   # lowest ask first
    return {"bids": bids, "asks": asks}


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_ticker(raw: dict) -> dict:
    """Map a /ticker/subscribe row into the dashboard's ticker schema.

    InnovestX returns 1-min OHLCV — `volume` is per-minute, not 24h.
    24h fields are left None for now and can be backfilled by accumulating
    polled history in SQLite (v2).
    """
    return {
        "venue": VENUE,
        "symbol": raw.get("symbol"),
        "last": _f(raw.get("close")),
        "bid": _f(raw.get("insideBidPrice")),
        "ask": _f(raw.get("insideAskPrice")),
        "high_24h": None,
        "low_24h": None,
        "base_volume_24h": None,
        "quote_turnover_24h": None,
        "change_pct_24h": None,
    }


def thb_symbols(symbols_payload: list[dict]) -> list[str]:
    """Filter the /symbols catalogue to THB-quoted pairs only.

    /symbols schema isn't documented field-by-field; we accept several common
    shapes (string list, {symbol: ...}, {symbolName: ...}) and keep only those
    ending in 'THB'.
    """
    out: list[str] = []
    for entry in symbols_payload or []:
        if isinstance(entry, str):
            sym = entry
        elif isinstance(entry, dict):
            sym = entry.get("symbol") or entry.get("symbolName") or entry.get("name") or ""
        else:
            continue
        if isinstance(sym, str) and sym.upper().endswith(QUOTE):
            out.append(sym.upper())
    return sorted(set(out))
