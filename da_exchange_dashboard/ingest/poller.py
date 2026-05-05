"""REST snapshot poller — Bitkub + Binance TH every POLL_INTERVAL_S seconds.

Run continuously:   python -m ingest.poller
Run one cycle:      python -m ingest.poller --once
"""
import logging
import sys
import time

from . import store
from .adapters import binance_th, bitkub, coingecko, innovestx, upbit_th

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("poller")

POLL_INTERVAL_S = 45
DEPTH_TOP_N = 5  # only fetch L2 for top-N highest-turnover symbols per venue


def _top_by_turnover(tickers: list[dict], n: int) -> list[dict]:
    return sorted(
        [t for t in tickers if t.get("quote_turnover_24h")],
        key=lambda t: t["quote_turnover_24h"],
        reverse=True,
    )[:n]


def poll_bitkub(conn) -> int:
    raw = bitkub.fetch_tickers()
    tickers = [bitkub.normalize_ticker(t) for t in raw]
    n = store.insert_tickers(conn, tickers)
    log.info("bitkub: %d tickers stored", n)
    for t in _top_by_turnover(tickers, DEPTH_TOP_N):
        try:
            depth = bitkub.fetch_depth(t["symbol"])
            store.insert_depth(conn, "bitkub", t["symbol"], depth)
        except Exception as e:
            log.warning("bitkub depth %s failed: %s", t["symbol"], e)
    return n


def poll_binance_th(conn) -> int:
    thb_syms = binance_th.fetch_thb_symbols()
    book = {b["symbol"]: b for b in binance_th.fetch_book_tickers()}
    tickers = []
    for sym in thb_syms:
        try:
            t24 = binance_th.fetch_24h(sym)
            tickers.append(binance_th.normalize_ticker(t24, book.get(sym)))
        except Exception as e:
            log.warning("binance_th 24hr %s failed: %s", sym, e)
    n = store.insert_tickers(conn, tickers)
    log.info("binance_th: %d THB tickers stored", n)
    for t in _top_by_turnover(tickers, DEPTH_TOP_N):
        try:
            depth = binance_th.fetch_depth(t["symbol"])
            store.insert_depth(conn, "binance_th", t["symbol"], depth)
        except Exception as e:
            log.warning("binance_th depth %s failed: %s", t["symbol"], e)
    return n


def poll_upbit_th(conn) -> int:
    markets = upbit_th.fetch_thb_markets()
    if not markets:
        log.warning("upbit_th: no THB markets discovered")
        return 0
    raw_tickers = upbit_th.fetch_tickers(markets)
    obs = {ob["market"]: ob for ob in upbit_th.fetch_orderbooks(markets)}
    tickers = [upbit_th.normalize_ticker(t, obs.get(t["market"])) for t in raw_tickers]
    n = store.insert_tickers(conn, tickers)
    log.info("upbit_th: %d THB tickers stored", n)
    for t in _top_by_turnover(tickers, DEPTH_TOP_N):
        ob = obs.get(t["symbol"])
        if ob:
            store.insert_depth(conn, "upbit_th", t["symbol"], upbit_th.normalize_depth(ob))
    return n


def poll_innovestx(conn) -> int:
    """Poll InnovestX (authenticated). Accumulates 1m bars and synthesizes
    24h figures locally because InvX REST exposes no native 24h endpoint.

    Skips silently when API creds are missing.
    """
    try:
        symbols_payload = innovestx.fetch_symbols()
    except innovestx.InnovestXAuthError:
        log.info("innovestx: skipped (INVX_API_KEY/INVX_API_SECRET not set)")
        return 0
    thb = innovestx.thb_symbols(symbols_payload)
    if not thb:
        log.warning("innovestx: no THB symbols discovered")
        return 0

    # Pass 1: fetch each symbol's latest 1m bar, store the bar, build the
    # base ticker dict (with 24h fields still None).
    tickers: list[dict] = []
    raw_by_sym: dict[str, dict] = {}
    for sym in thb:
        try:
            raw = innovestx.fetch_ticker(sym)
            if raw:
                raw_by_sym[sym] = raw
                store.insert_invx_bar(conn, sym, raw)
                tickers.append(innovestx.normalize_ticker(raw))
        except Exception as e:
            log.warning("innovestx ticker %s failed: %s", sym, e)

    # Pass 2: prune old bars + synthesize 24h figures from the rolling window.
    try:
        store.prune_invx_bars(conn, hours=25)
    except Exception as e:
        log.warning("innovestx prune failed: %s", e)
        conn.rollback()

    synthesized = 0
    for t in tickers:
        try:
            agg = store.synthesize_invx_24h(conn, t["symbol"])
            t["base_volume_24h"] = agg["base_volume_24h"]
            t["quote_turnover_24h"] = agg["quote_turnover_24h"]
            t["high_24h"] = agg["high_24h"]
            t["low_24h"] = agg["low_24h"]
            t["change_pct_24h"] = agg["change_pct_24h"]
            if agg["quote_turnover_24h"] is not None:
                synthesized += 1
        except Exception as e:
            log.warning("innovestx synthesis %s failed: %s", t["symbol"], e)
            conn.rollback()

    n = store.insert_tickers(conn, tickers)
    log.info("innovestx: %d THB tickers stored (%d with synth 24h)", n, synthesized)

    # L2 depth for top-N by synthesized turnover (or first N if no turnover yet)
    for t in _top_by_turnover(tickers, DEPTH_TOP_N) or tickers[:DEPTH_TOP_N]:
        try:
            depth = innovestx.fetch_depth(t["symbol"])
            store.insert_depth(conn, "innovestx", t["symbol"], depth)
        except Exception as e:
            log.warning("innovestx depth %s failed: %s", t["symbol"], e)
    return n


def poll_coingecko_venue(conn, venue_label: str, exchange_id: str) -> int:
    raw = coingecko.fetch_tickers(exchange_id)
    tickers = coingecko.normalize_thb_tickers(raw, venue_label)
    n = store.insert_tickers(conn, tickers)
    log.info("%s (cg=%s): %d THB tickers stored", venue_label, exchange_id, n)
    return n


def run_once() -> None:
    # psycopg2 puts the connection into 'aborted' state on any DB error and
    # refuses subsequent statements until ROLLBACK. Without conn.rollback()
    # in each except branch below, one venue's failure poisoned the entire
    # cycle (observed 2026-05-02 to 05 when Neon hit the 512MB cap).
    conn = store.get_conn()
    try:
        for name, fn in [
            ("bitkub", poll_bitkub),
            ("binance_th", poll_binance_th),
            ("upbit_th", poll_upbit_th),
            ("innovestx", poll_innovestx),
        ]:
            try:
                fn(conn)
            except Exception as e:
                log.error("%s poll failed: %s", name, e)
                conn.rollback()
        for venue_label, exchange_id in coingecko.EXCHANGE_IDS.items():
            try:
                poll_coingecko_venue(conn, venue_label, exchange_id)
            except Exception as e:
                log.error("%s (cg) poll failed: %s", venue_label, e)
                conn.rollback()
        try:
            latest = store.latest_tickers(conn)
            written = store.upsert_daily_turnover(conn, latest)
            log.info("daily_turnover: rolled up %d rows", written)
        except Exception as e:
            log.error("daily_turnover rollup failed: %s", e)
            _safe_rollback(conn)
    finally:
        conn.close()


def run_forever() -> None:
    log.info("Poller started, interval=%ds", POLL_INTERVAL_S)
    while True:
        run_once()
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        run_forever()
