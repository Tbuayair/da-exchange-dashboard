"""Backend-agnostic DB connection layer.

Picks PostgreSQL when DATABASE_URL is set (production: Neon), otherwise falls
back to local SQLite (dev). Both backends expose DB-API 2.0, but they differ on:
  - placeholder syntax: SQLite uses '?', psycopg2 uses '%s'
  - upsert: SQLite supports INSERT OR REPLACE, Postgres uses ON CONFLICT
  - bool/int truthiness on returned rows

We translate '?' placeholders to '%s' on the way out for psycopg2 so call sites
can write a single SQL string. Schema uses ON CONFLICT (which both support
since SQLite 3.24+ / Postgres 9.5+).
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

# SQLite path (legacy) — only consulted when USE_POSTGRES is False.
_DEFAULT_SQLITE = Path(__file__).parent.parent / "data" / "snapshots.db"
SQLITE_PATH = Path(os.environ.get("DA_DB_PATH", _DEFAULT_SQLITE))

if USE_POSTGRES:
    import psycopg2  # type: ignore[import-not-found]


class _ConnWrapper:
    """Thin wrapper that hides driver differences from store.py.

    Exposes execute / executemany / commit / close / cursor and translates '?'
    placeholders to '%s' when running on psycopg2. fetchall returns tuples in
    both backends, so callers convert to dicts via cursor.description.
    """

    def __init__(self, conn, is_postgres: bool):
        self._conn = conn
        self._pg = is_postgres

    def _translate(self, sql: str) -> str:
        if self._pg:
            # naive but safe: placeholders are always literal '?', never inside
            # quoted strings in our queries (verified by reading store.py).
            return sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: tuple | list | None = None):
        cur = self._conn.cursor()
        cur.execute(self._translate(sql), params or ())
        return cur

    def executemany(self, sql: str, rows):
        cur = self._conn.cursor()
        cur.executemany(self._translate(sql), rows)
        return cur

    def executescript(self, sql: str):
        """Run a multi-statement DDL script. SQLite has executescript natively;
        on Postgres we split on ';' and run each statement."""
        if not self._pg:
            self._conn.executescript(sql)
            return
        cur = self._conn.cursor()
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        self._conn.commit()

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def cursor(self):
        return self._conn.cursor()


def connect() -> _ConnWrapper:
    """Open a connection to the configured backend."""
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        return _ConnWrapper(conn, is_postgres=True)
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH)
    return _ConnWrapper(conn, is_postgres=False)


@contextmanager
def session():
    """Context manager — auto-closes the connection."""
    c = connect()
    try:
        yield c
    finally:
        c.close()


def backend_label() -> str:
    return "postgres" if USE_POSTGRES else f"sqlite({SQLITE_PATH})"
