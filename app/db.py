"""SQLite schema + helpers for Munshi (Phase 1).

Boring stdlib sqlite3 only. No ORM.
Every table from the Phase 1 spec. Every agent step later
writes to `events`; Phase 1 just creates the tables.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# Project root = parent of app/. DB lives at data/munshi.db.
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "munshi.db"


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Return the sqlite file to use (explicit arg > env > default)."""
    if db_path is not None:
        return Path(db_path)
    env = os.environ.get("MUNSHI_DB")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


def get_conn(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with Row factory + FK enforcement."""
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


# -- schema -----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    unit TEXT NOT NULL,
    price REAL NOT NULL,
    stock_qty REAL NOT NULL,
    reorder_level REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    credit_limit REAL NOT NULL DEFAULT 0,
    outstanding REAL NOT NULL DEFAULT 0,
    is_new INTEGER NOT NULL DEFAULT 0,
    usual_basket_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'packer', 'delivery'))
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL,
    transcript TEXT NOT NULL DEFAULT '',
    total REAL NOT NULL DEFAULT 0,
    payment_mode TEXT,
    packer_id INTEGER REFERENCES staff(id),
    delivery_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty REAL NOT NULL,
    unit_price REAL NOT NULL,
    packed_qty REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER REFERENCES customers(id),
    order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    direction TEXT NOT NULL,
    text TEXT NOT NULL,
    audio_path TEXT,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    staff_id INTEGER REFERENCES staff(id),
    order_id INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    action_required INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_lines_order ON order_lines(order_id);
CREATE INDEX IF NOT EXISTS idx_lines_item ON order_lines(item_id);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id);
CREATE INDEX IF NOT EXISTS idx_messages_customer ON messages(customer_id);
CREATE INDEX IF NOT EXISTS idx_notifications_role ON notifications(role, done);
"""

# Tables wiped by reset_demo.py (transactional/demo data, not masters).
DEMO_TABLES = ("notifications", "messages", "events", "order_lines", "orders")

# All tables for row-count reporting.
ALL_TABLES = (
    "items",
    "customers",
    "staff",
    "orders",
    "order_lines",
    "events",
    "messages",
    "notifications",
)


def init_db(db_path: str | Path | None = None) -> Path:
    """Create all tables/indexes. Idempotent. Returns db path."""
    path = resolve_db_path(db_path)
    conn = get_conn(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return path


def table_counts(db_path: str | Path | None = None) -> dict[str, int]:
    """Return {table: row_count} for every known table."""
    conn = get_conn(db_path)
    try:
        counts: dict[str, int] = {}
        for t in ALL_TABLES:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
            counts[t] = int(row["n"])
        return counts
    finally:
        conn.close()
