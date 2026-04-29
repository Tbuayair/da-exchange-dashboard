"""REST snapshot poller — Bitkub + Binance TH every POLL_INTERVAL_S seconds.

Run continuously:   python -m ingest.poller
Run one cycle:      python -m ingest.poller --once
"""
import logging
import sys
import time

from . import store
from .adapters import binance_th, bitkub, coingecko, upbit_th

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


def poll_coingecko_venue(conn, venue_label: str, exchange_id: str) -> int:
    raw = coingecko.fetch_tickers(exchange_id)
    tickers = coingecko.normalize_thb_tickers(raw, venue_label)
    n = store.insert_tickers(conn, tickers)
    log.info("%s (cg=%s): %d THB tickers stored", venue_label, exchange_id, n)
    return n


def run_once() -> None:
    conn = store.get_conn()
    try:
        for name, fn in [
            ("bitkub", poll_bitkub),
            ("binance_th", poll_binance_th),
            ("upbit_th", poll_upbit_th),
        ]:
            try:
                fn(conn)
            except Exception as e:
                log.error("%s poll failed: %s", name, e)
        for venue_label, exchange_id in coingecko.EXCHANGE_IDS.items():
            try:
                poll_coingecko_venue(conn, venue_label, exchange_id)
            except Exception as e:
                log.error("%s (cg) poll failed: %s", venue_label, e)
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
