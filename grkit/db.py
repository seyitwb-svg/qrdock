"""SQLite helpers: WAL mode, schema bootstrap, per-request connections."""
from __future__ import annotations

import os
import sqlite3
import threading

DB_PATH = os.environ.get("GR_DB_PATH", "/data/app.db")

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    notify_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
    plan TEXT NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    ref TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT,
    at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect() -> sqlite3.Connection:
    con = getattr(_local, "con", None)
    if con is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        con = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        con.executescript(SCHEMA)
        for col, ddl in (("notify_url", "TEXT NOT NULL DEFAULT ''"),):
            try:
                con.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass
        _local.con = con
    return con


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def exec_(sql: str, params: tuple = ()) -> int:
    cur = connect().execute(sql, params)
    connect().commit()
    return cur.lastrowid


def log_event(user_id, kind: str, detail: str = "") -> None:
    exec_("INSERT INTO events(user_id,kind,detail) VALUES(?,?,?)", (user_id, kind, detail[:500]))
