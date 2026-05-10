"""Backend-agnostic snapshot store — LATEST-ONLY tickers + depth + daily rollup.

Storage strategy (v2, post 2026-05-05 redesign):

- `latest_ticker` keeps exactly ONE row per (venue, symbol) — the most recent
  reading. Each poll cycle UPSERTs and replaces, so the table is small forever
  (~520 rows total ≈ <1 MB). Per-snapshot history is intentionally not kept;
  the dashboard only ever queries the latest reading anyway.
- `latest_depth` similarly keeps one L2 snapshot per (venue, symbol).
- `daily_turnover` is the ONLY table that grows over time (one row per
  (date, venue, symbol)) — this is the durable history we care about.

Backend-portable SQL: uses `INSERT ... ON CONFLICT DO UPDATE` (works on both
SQLite 3.24+ and Postgres 9.5+).
"""
import json
from datetime import datetime, timedelta, timezone

from . import db


# v2 schema. Old `ticker_snapshots` and `depth_snapshots` (with ts in PK) are
# orphaned — drop them manually after this lands. The DROP is intentionally NOT
# in this script so accidental re-imports don't destroy local dev DBs.
SCHEMA = """
CREATE TABLE IF NOT EXISTS latest_ticker (
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    last DOUBLE PRECISION,
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    high_24h DOUBLE PRECISION,
    low_24h DOUBLE PRECISION,
    base_volume_24h DOUBLE PRECISION,
    quote_turnover_24h DOUBLE PRECISION,
    change_pct_24h DOUBLE PRECISION,
    PRIMARY KEY (venue, symbol)
);

CREATE TABLE IF NOT EXISTS latest_depth (
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    bids_json TEXT NOT NULL,
    asks_json TEXT NOT NULL,
    PRIMARY KEY (venue, symbol)
);

-- One row per (date, venue, symbol). Persists across redeploys (Postgres on Neon).
CREATE TABLE IF NOT EXISTS daily_turnover (
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    last_ts TEXT NOT NULL,
    last_price DOUBLE PRECISION,
    base_volume_24h DOUBLE PRECISION,
    quote_turnover_24h DOUBLE PRECISION,
    change_pct_24h DOUBLE PRECISION,
    PRIMARY KEY (date, venue, symbol)
);
CREATE INDEX IF NOT EXISTS idx_daily_venue_symbol ON daily_turnover(venue, symbol);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_turnover(date);

-- 1-minute OHLCV bars for InnovestX. Used to synthesize 24h figures locally
-- because InvX's REST API doesn't expose 24h-rolling stats directly. Pruned
-- to ~25 hours of history each cycle (~52K rows steady state, <5 MB).
CREATE TABLE IF NOT EXISTS invx_bars_1m (
    ts_minute TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open DOUBLE PRECISION,
    high DOUBLE PRECISION,
    low DOUBLE PRECISION,
    close DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    PRIMARY KEY (ts_minute, symbol)
);
CREATE INDEX IF NOT EXISTS idx_invx_bars_symbol_ts ON invx_bars_1m(symbol, ts_minute);
"""


def get_conn():
    conn = db.connect()
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_tickers(conn, tickers: list[dict], ts: str | None = None) -> int:
    """UPSERT — replaces the latest row per (venue, symbol). No history kept."""
    ts = ts or now_iso()
    rows = [
        (
            t["venue"], t["symbol"], ts,
            t.get("last"), t.get("bid"), t.get("ask"),
            t.get("high_24h"), t.get("low_24h"),
            t.get("base_volume_24h"), t.get("quote_turnover_24h"),
            t.get("change_pct_24h"),
        )
        for t in tickers if t.get("symbol")
    ]
    conn.executemany(
        """
        INSERT INTO latest_ticker
            (venue, symbol, ts, last, bid, ask, high_24h, low_24h,
             base_volume_24h, quote_turnover_24h, change_pct_24h)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (venue, symbol) DO UPDATE SET
            ts=excluded.ts,
            last=excluded.last, bid=excluded.bid, ask=excluded.ask,
            high_24h=excluded.high_24h, low_24h=excluded.low_24h,
            base_volume_24h=excluded.base_volume_24h,
            quote_turnover_24h=excluded.quote_turnover_24h,
            change_pct_24h=excluded.change_pct_24h
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def insert_depth(conn, venue: str, symbol: str, depth: dict, ts: str | None = None) -> None:
    """UPSERT — replaces the latest L2 snapshot per (venue, symbol)."""
    ts = ts or now_iso()
    conn.execute(
        """
        INSERT INTO latest_depth (venue, symbol, ts, bids_json, asks_json)
        VALUES (?,?,?,?,?)
        ON CONFLICT (venue, symbol) DO UPDATE SET
            ts=excluded.ts,
            bids_json=excluded.bids_json,
            asks_json=excluded.asks_json
        """,
        (
            venue, symbol, ts,
            json.dumps(depth.get("bids", [])),
            json.dumps(depth.get("asks", [])),
        ),
    )
    conn.commit()


def latest_tickers(conn, venue: str | None = None) -> list[dict]:
    """All current tickers — one row per (venue, symbol). Trivial SELECT now."""
    sql = """
    SELECT ts, venue, symbol, last, bid, ask, high_24h, low_24h,
           base_volume_24h, quote_turnover_24h, change_pct_24h
    FROM latest_ticker
    """
    args: tuple = ()
    if venue:
        sql += " WHERE venue = ?"
        args = (venue,)
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def latest_all_depth(conn) -> list[tuple]:
    """Returns (venue, symbol, bids_json, asks_json) for every (venue, symbol)."""
    cur = conn.execute(
        "SELECT venue, symbol, bids_json, asks_json FROM latest_depth"
    )
    return cur.fetchall()


def latest_depth(conn, venue: str, symbol: str) -> dict | None:
    cur = conn.execute(
        "SELECT ts, bids_json, asks_json FROM latest_depth "
        "WHERE venue = ? AND symbol = ?",
        (venue, symbol),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"ts": row[0], "bids": json.loads(row[1]), "asks": json.loads(row[2])}


def _utc_date(ts_iso: str) -> str:
    return ts_iso[:10]


def upsert_daily_turnover(conn, tickers: list[dict], ts: str | None = None) -> int:
    """Roll the latest 24h-rolling figures into one row per (date, venue, symbol).

    Called every poll cycle — each call overwrites today's row, so the last
    write before UTC midnight becomes the de-facto end-of-day record.

    Single-row helper using a list of ticker dicts. Prefer
    upsert_daily_turnover_from_latest() for hot paths — it does the rollup
    entirely server-side with zero egress.
    """
    ts = ts or now_iso()
    date = _utc_date(ts)
    rows = [
        (
            date, t["venue"], t["symbol"], ts,
            t.get("last"),
            t.get("base_volume_24h"),
            t.get("quote_turnover_24h"),
            t.get("change_pct_24h"),
        )
        for t in tickers if t.get("symbol")
    ]
    conn.executemany(
        """
        INSERT INTO daily_turnover
            (date, venue, symbol, last_ts, last_price,
             base_volume_24h, quote_turnover_24h, change_pct_24h)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT (date, venue, symbol) DO UPDATE SET
            last_ts=excluded.last_ts,
            last_price=excluded.last_price,
            base_volume_24h=excluded.base_volume_24h,
            quote_turnover_24h=excluded.quote_turnover_24h,
            change_pct_24h=excluded.change_pct_24h
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_daily_turnover_from_latest(conn) -> int:
    """Roll latest_ticker → daily_turnover ENTIRELY server-side.

    Replaces the prior pattern of (a) SELECT * FROM latest_ticker [egress
    ~65 KB/cycle], (b) build dict list in app, (c) executemany INSERT.
    The new SQL pulls 0 bytes across the wire — Postgres reads its own
    rows and writes the result row in the same query plan.

    Returns the number of rows affected if the driver reports it, else -1.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    ts = now_iso()
    cur = conn.execute(
        """
        INSERT INTO daily_turnover
            (date, venue, symbol, last_ts, last_price,
             base_volume_24h, quote_turnover_24h, change_pct_24h)
        SELECT ?, venue, symbol, ?, last,
               base_volume_24h, quote_turnover_24h, change_pct_24h
        FROM latest_ticker
        WHERE 1=1
        ON CONFLICT (date, venue, symbol) DO UPDATE SET
            last_ts=excluded.last_ts,
            last_price=excluded.last_price,
            base_volume_24h=excluded.base_volume_24h,
            quote_turnover_24h=excluded.quote_turnover_24h,
            change_pct_24h=excluded.change_pct_24h
        """,
        (today, ts),
    )
    conn.commit()
    n = getattr(cur, "rowcount", -1)
    return n if n is not None else -1


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def insert_invx_bar(conn, symbol: str, bar: dict) -> bool:
    """Append a single 1-minute bar to invx_bars_1m. Single-row helper.

    Prefer insert_invx_bars_batch() for hot paths — it batches into one
    transaction per cycle instead of one transaction per symbol.
    """
    ts_minute = bar.get("dateTime")
    if not ts_minute:
        return False
    conn.execute(
        """
        INSERT INTO invx_bars_1m
            (ts_minute, symbol, open, high, low, close, volume, bid, ask)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT (ts_minute, symbol) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume,
            bid=excluded.bid, ask=excluded.ask
        """,
        (
            ts_minute, symbol,
            _f(bar.get("open")), _f(bar.get("high")),
            _f(bar.get("low")), _f(bar.get("close")),
            _f(bar.get("volume")),
            _f(bar.get("insideBidPrice")), _f(bar.get("insideAskPrice")),
        ),
    )
    conn.commit()
    return True


def insert_invx_bars_batch(conn, bars_by_symbol: dict[str, dict]) -> int:
    """Batch-insert multiple 1m bars in a single transaction.

    bars_by_symbol: {symbol: bar_dict_from_invx_api}. Returns count of rows
    actually written (skips entries missing dateTime).
    """
    rows = []
    for symbol, bar in bars_by_symbol.items():
        ts_minute = bar.get("dateTime") if bar else None
        if not ts_minute:
            continue
        rows.append((
            ts_minute, symbol,
            _f(bar.get("open")), _f(bar.get("high")),
            _f(bar.get("low")), _f(bar.get("close")),
            _f(bar.get("volume")),
            _f(bar.get("insideBidPrice")), _f(bar.get("insideAskPrice")),
        ))
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO invx_bars_1m
            (ts_minute, symbol, open, high, low, close, volume, bid, ask)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT (ts_minute, symbol) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume,
            bid=excluded.bid, ask=excluded.ask
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def prune_invx_bars(conn, hours: int = 25) -> int:
    """Delete InvX 1m bars older than `hours`. Keeps ~25h by default to make
    24h synthesis robust against minor clock skew.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    cur = conn.execute(
        "DELETE FROM invx_bars_1m WHERE ts_minute < ?",
        (cutoff,),
    )
    conn.commit()
    # rowcount may be -1 on some drivers; treat that as unknown.
    n = getattr(cur, "rowcount", -1)
    return n if n is not None and n >= 0 else 0


_EMPTY_SYNTH = {
    "base_volume_24h": None, "quote_turnover_24h": None,
    "high_24h": None, "low_24h": None, "change_pct_24h": None,
}


def synthesize_invx_24h_all(conn, hours: int = 24) -> dict[str, dict]:
    """Batch-aggregate 24h figures for ALL InvX symbols in a single round-trip.

    Server-side GROUP BY transfers only ~35 result rows (~3 KB) instead of
    pulling ~50,000 bar rows (~2.5 MB) per cycle. Critical for staying inside
    Neon free-tier egress (5 GB/month).

    Quote turnover approximated as SUM(volume × close); exact VWAP would need
    trade-level data we don't have. change_pct_24h derived from first/last
    close inside the window via correlated subqueries that hit indexed
    (symbol, ts_minute) PK lookups — both Postgres and SQLite plan these as
    index scans, no extra egress.

    Returns: {symbol: {base_volume_24h, quote_turnover_24h, high_24h,
    low_24h, change_pct_24h}}. Symbols with no bars are absent from the dict.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(hours))).isoformat(timespec="seconds")
    cur = conn.execute(
        """
        WITH agg AS (
            SELECT symbol,
                   SUM(volume)         AS bv,
                   SUM(volume * close) AS qto,
                   MAX(high)           AS hi,
                   MIN(low)            AS lo,
                   MIN(ts_minute)      AS first_ts,
                   MAX(ts_minute)      AS last_ts,
                   COUNT(*)            AS n
            FROM invx_bars_1m
            WHERE ts_minute >= ?
            GROUP BY symbol
        )
        SELECT a.symbol, a.bv, a.qto, a.hi, a.lo, a.n,
               (SELECT close FROM invx_bars_1m b
                 WHERE b.symbol = a.symbol AND b.ts_minute = a.first_ts) AS first_close,
               (SELECT close FROM invx_bars_1m b
                 WHERE b.symbol = a.symbol AND b.ts_minute = a.last_ts) AS last_close
        FROM agg a
        """,
        (cutoff,),
    )
    out: dict[str, dict] = {}
    for row in cur.fetchall():
        symbol, bv, qto, hi, lo, n, first_close, last_close = row
        change_pct = None
        if n and n >= 2 and first_close:
            try:
                change_pct = (last_close - first_close) / first_close * 100
            except (TypeError, ZeroDivisionError):
                change_pct = None
        out[symbol] = {
            "base_volume_24h": float(bv) if bv is not None else None,
            "quote_turnover_24h": float(qto) if qto is not None else None,
            "high_24h": float(hi) if hi is not None else None,
            "low_24h": float(lo) if lo is not None else None,
            "change_pct_24h": change_pct,
        }
    return out


def synthesize_invx_24h(conn, symbol: str) -> dict:
    """Single-symbol shim around synthesize_invx_24h_all() for compatibility.

    DEPRECATED in hot paths — prefer synthesize_invx_24h_all() for batch
    operation. Each call here triggers a full GROUP BY scan, so calling it
    in a loop reverts the egress optimization.
    """
    return synthesize_invx_24h_all(conn).get(symbol, dict(_EMPTY_SYNTH))


def turnover_share_by_venue(conn, date_str: str | None = None) -> dict:
    """Total quote turnover per venue on a given UTC date.

    Aggregates rows in `daily_turnover` for the requested date, summing
    `quote_turnover_24h` across all symbols within each venue. Returns a
    dict ready for chart consumption (pie/donut).

    Args:
        date_str: 'YYYY-MM-DD' (UTC). Defaults to today (UTC).

    Returns:
        {
            'date': '2026-05-05',
            'total': 1234567890.0,
            'venues': [
                {'venue': 'bitkub', 'total': 800_000_000.0, 'share_pct': 64.8},
                ...sorted desc by total
            ]
        }
    """
    if not date_str:
        date_str = datetime.now(timezone.utc).date().isoformat()
    cur = conn.execute(
        """
        SELECT venue, SUM(quote_turnover_24h) AS total
        FROM daily_turnover
        WHERE date = ? AND quote_turnover_24h IS NOT NULL
        GROUP BY venue
        ORDER BY total DESC
        """,
        (date_str,),
    )
    rows = cur.fetchall()
    venues = [{"venue": r[0], "total": float(r[1] or 0)} for r in rows]
    total = sum(v["total"] for v in venues)
    for v in venues:
        v["share_pct"] = round((v["total"] / total * 100), 2) if total else 0.0
    return {"date": date_str, "total": total, "venues": venues}


def turnover_share_dates(conn, days: int = 30) -> list[str]:
    """List UTC dates (newest first) that have turnover data, capped at `days`."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=int(days) - 1)).isoformat()
    cur = conn.execute(
        """
        SELECT DISTINCT date FROM daily_turnover
        WHERE date >= ? AND quote_turnover_24h IS NOT NULL
        ORDER BY date DESC
        """,
        (cutoff,),
    )
    return [r[0] for r in cur.fetchall()]


def daily_turnover_history(
    conn,
    venue: str | None = None,
    symbol: str | None = None,
    days: int = 30,
) -> list[dict]:
    """Return historical daily rows from the last `days` days, newest first."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=int(days) - 1)).isoformat()
    where = ["date >= ?"]
    args: list = [cutoff]
    if venue:
        where.append("venue = ?")
        args.append(venue)
    if symbol:
        where.append("symbol = ?")
        args.append(symbol)
    sql = f"""
    SELECT date, venue, symbol, last_ts, last_price,
           base_volume_24h, quote_turnover_24h, change_pct_24h
    FROM daily_turnover
    WHERE {" AND ".join(where)}
    ORDER BY date DESC, venue, symbol
    """
    cur = conn.execute(sql, tuple(args))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
