"""Backend-agnostic snapshot store for ticker (L1+turnover) and depth (L2) data.

Uses db.py to pick PostgreSQL (production) or SQLite (dev) based on
DATABASE_URL. Schema uses ON CONFLICT (works in SQLite 3.24+ and Postgres 9.5+)
so the same SQL runs on both backends.
"""
import json
from datetime import datetime, timedelta, timezone

from . import db


SCHEMA = """
CREATE TABLE IF NOT EXISTS ticker_snapshots (
    ts TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    last DOUBLE PRECISION,
    bid DOUBLE PRECISION,
    ask DOUBLE PRECISION,
    high_24h DOUBLE PRECISION,
    low_24h DOUBLE PRECISION,
    base_volume_24h DOUBLE PRECISION,
    quote_turnover_24h DOUBLE PRECISION,
    change_pct_24h DOUBLE PRECISION,
    PRIMARY KEY (ts, venue, symbol)
);
CREATE INDEX IF NOT EXISTS idx_ticker_venue_symbol ON ticker_snapshots(venue, symbol);
CREATE INDEX IF NOT EXISTS idx_ticker_ts ON ticker_snapshots(ts);

CREATE TABLE IF NOT EXISTS depth_snapshots (
    ts TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bids_json TEXT NOT NULL,
    asks_json TEXT NOT NULL,
    PRIMARY KEY (ts, venue, symbol)
);
CREATE INDEX IF NOT EXISTS idx_depth_venue_symbol ON depth_snapshots(venue, symbol);

-- One row per (date, venue, symbol). Persists across redeploys when the
-- underlying database is durable (Postgres on Neon).
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
"""


def get_conn():
    conn = db.connect()
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_tickers(conn, tickers: list[dict], ts: str | None = None) -> int:
    ts = ts or now_iso()
    rows = [
        (
            ts, t["venue"], t["symbol"],
            t.get("last"), t.get("bid"), t.get("ask"),
            t.get("high_24h"), t.get("low_24h"),
            t.get("base_volume_24h"), t.get("quote_turnover_24h"),
            t.get("change_pct_24h"),
        )
        for t in tickers if t.get("symbol")
    ]
    conn.executemany(
        """
        INSERT INTO ticker_snapshots
            (ts, venue, symbol, last, bid, ask, high_24h, low_24h,
             base_volume_24h, quote_turnover_24h, change_pct_24h)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (ts, venue, symbol) DO UPDATE SET
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
    ts = ts or now_iso()
    conn.execute(
        """
        INSERT INTO depth_snapshots (ts, venue, symbol, bids_json, asks_json)
        VALUES (?,?,?,?,?)
        ON CONFLICT (ts, venue, symbol) DO UPDATE SET
            bids_json=excluded.bids_json, asks_json=excluded.asks_json
        """,
        (
            ts, venue, symbol,
            json.dumps(depth.get("bids", [])),
            json.dumps(depth.get("asks", [])),
        ),
    )
    conn.commit()


def latest_tickers(conn, venue: str | None = None) -> list[dict]:
    """Most-recent ticker per (venue, symbol)."""
    sql = """
    SELECT t.ts, t.venue, t.symbol, t.last, t.bid, t.ask, t.high_24h, t.low_24h,
           t.base_volume_24h, t.quote_turnover_24h, t.change_pct_24h
    FROM ticker_snapshots t
    JOIN (
        SELECT venue, symbol, MAX(ts) AS max_ts
        FROM ticker_snapshots
        GROUP BY venue, symbol
    ) m ON m.venue = t.venue AND m.symbol = t.symbol AND m.max_ts = t.ts
    """
    args: tuple = ()
    if venue:
        sql += " WHERE t.venue = ?"
        args = (venue,)
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def latest_depth(conn, venue: str, symbol: str) -> dict | None:
    cur = conn.execute(
        "SELECT ts, bids_json, asks_json FROM depth_snapshots "
        "WHERE venue = ? AND symbol = ? ORDER BY ts DESC LIMIT 1",
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
