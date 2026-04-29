"""SQLite snapshot store for ticker (L1+turnover) and depth (L2) data."""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Allow override for deployments where the package lives in a read-only location
# (e.g. claude-projects on Koyeb where this repo is pip-installed into site-packages).
_DEFAULT_DB = Path(__file__).parent.parent / "data" / "snapshots.db"
DB_PATH = Path(os.environ.get("DA_DB_PATH", _DEFAULT_DB))

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticker_snapshots (
    ts TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    last REAL,
    bid REAL,
    ask REAL,
    high_24h REAL,
    low_24h REAL,
    base_volume_24h REAL,
    quote_turnover_24h REAL,
    change_pct_24h REAL,
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

-- One row per (date, venue, symbol). Persists across redeploys when DB is on a Koyeb volume.
-- Each cycle overwrites today's row with the latest 24h-rolling figures, so the row written
-- closest to UTC midnight is the de-facto end-of-day snapshot.
CREATE TABLE IF NOT EXISTS daily_turnover (
    date TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    last_ts TEXT NOT NULL,
    last_price REAL,
    base_volume_24h REAL,
    quote_turnover_24h REAL,
    change_pct_24h REAL,
    PRIMARY KEY (date, venue, symbol)
);
CREATE INDEX IF NOT EXISTS idx_daily_venue_symbol ON daily_turnover(venue, symbol);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_turnover(date);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_tickers(conn: sqlite3.Connection, tickers: list[dict], ts: str | None = None) -> int:
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
        "INSERT OR REPLACE INTO ticker_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def insert_depth(
    conn: sqlite3.Connection,
    venue: str,
    symbol: str,
    depth: dict,
    ts: str | None = None,
) -> None:
    ts = ts or now_iso()
    conn.execute(
        "INSERT OR REPLACE INTO depth_snapshots VALUES (?,?,?,?,?)",
        (
            ts, venue, symbol,
            json.dumps(depth.get("bids", [])),
            json.dumps(depth.get("asks", [])),
        ),
    )
    conn.commit()


def latest_tickers(conn: sqlite3.Connection, venue: str | None = None) -> list[dict]:
    """Most-recent ticker per (venue, symbol)."""
    sql = """
    SELECT t.* FROM ticker_snapshots t
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


def _utc_date(ts_iso: str) -> str:
    return ts_iso[:10]


def upsert_daily_turnover(
    conn: sqlite3.Connection,
    tickers: list[dict],
    ts: str | None = None,
) -> int:
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
        "INSERT OR REPLACE INTO daily_turnover VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def daily_turnover_history(
    conn: sqlite3.Connection,
    venue: str | None = None,
    symbol: str | None = None,
    days: int = 30,
) -> list[dict]:
    """Return historical daily rows from the last `days` days, newest first.

    Optionally filter by venue/symbol.
    """
    from datetime import timedelta
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


def latest_depth(conn: sqlite3.Connection, venue: str, symbol: str) -> dict | None:
    cur = conn.execute(
        "SELECT ts, bids_json, asks_json FROM depth_snapshots "
        "WHERE venue = ? AND symbol = ? ORDER BY ts DESC LIMIT 1",
        (venue, symbol),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"ts": row[0], "bids": json.loads(row[1]), "asks": json.loads(row[2])}
