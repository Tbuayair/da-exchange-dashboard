"""Orbix adapter — DEFERRED.

Probed 2026-04-28. None of the candidate endpoints work without auth/partnership:
  - api.orbixtrade.com  → DNS does not resolve
  - api.tdax.com        → TLS SNI mismatch (cert does not match host)
  - orbixtrade.com/api/instruments → 302 → www host returns 404

Until Anthony confirms the correct base URL and obtains API credentials,
Orbix coverage is left to the CoinGecko fallback (Phase 3) if Orbix is
listed there, or omitted from the dashboard.

When credentials are obtained, the expected shape of this module mirrors
bitkub.py: fetch_tickers(), normalize_ticker(raw), fetch_depth(symbol).
"""

VENUE = "orbix"
AVAILABLE = False
