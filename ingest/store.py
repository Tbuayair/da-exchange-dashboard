"""SQLite snapshot store for ticker (L1+turnover) and depth (L2) data."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "snapshots.db"

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
