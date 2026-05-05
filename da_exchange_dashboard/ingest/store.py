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


def insert_invx_bar(conn, symbol: str, bar: dict) -> bool:
    """Append a 1-minute bar from InvX /ticker/subscribe to invx_bars_1m.

    Idempotent on (ts_minute, symbol). The InvX API returns the same bar's
    dateTime each time within a minute, so reposting is harmless. Returns
    True if a row was written/updated.
    """
    ts_minute = bar.get("dateTime")
    if not ts_minute:
        return False

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

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


def synthesize_invx_24h(conn, symbol: str) -> dict:
    """Compute 24h-rolling figures for one InvX symbol from the last 1440 bars.

    Returns a dict with keys: base_volume_24h, quote_turnover_24h, high_24h,
    low_24h, change_pct_24h. Any field that can't be computed (e.g. <2 bars)
    is None. Quote turnover is approximated as SUM(volume × close) — exact
    VWAP would need trade-level data we don't have.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    cur = conn.execute(
        """
        SELECT close, volume, high, low
        FROM invx_bars_1m
        WHERE symbol = ? AND ts_minute >= ?
        ORDER BY ts_minute ASC
        """,
        (symbol, cutoff),
    )
    rows = cur.fetchall()
    if not rows:
        return {
            "base_volume_24h": None, "quote_turnover_24h": None,
            "high_24h": None, "low_24h": None, "change_pct_24h": None,
        }
    closes = [r[0] for r in rows if r[0] is not None]
    vols = [r[1] for r in rows if r[1] is not None]
    highs = [r[2] for r in rows if r[2] is not None]
    lows = [r[3] for r in rows if r[3] is not None]
    base_vol = sum(vols) if vols else None
    quote_turn = sum((r[0] or 0) * (r[1] or 0) for r in rows) if rows else None
    high = max(highs) if highs else None
    low = min(lows) if lows else None
    if len(closes) >= 2 and closes[0]:
        change_pct = (closes[-1] - closes[0]) / closes[0] * 100
    else:
        change_pct = None
    return {
        "base_volume_24h": base_vol,
        "quote_turnover_24h": quote_turn,
        "high_24h": high,
        "low_24h": low,
        "change_pct_24h": change_pct,
    }


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
