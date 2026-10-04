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
    """Open a connection with Row factory + FK enforcement.

    WAL mode + busy timeout: the web UI polls (reads) every 2s while the
    pipeline worker writes. Without WAL, a poll overlapping a write fails
    the write with "database is locked".
    """
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
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
    pending_json TEXT, -- Phase 4: clarification question parked on the order
    track_token TEXT, -- supervisor: unguessable tracking link token
    stage_entered_at TEXT, -- supervisor: when the current status began
    eta_at TEXT, -- supervisor: customer-facing ETA, set on delay notices
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
CREATE INDEX IF NOT EXISTS idx_orders_track ON orders(track_token);
CREATE INDEX IF NOT EXISTS idx_lines_order ON order_lines(order_id);
CREATE INDEX IF NOT EXISTS idx_lines_item ON order_lines(item_id);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id);
CREATE INDEX IF NOT EXISTS idx_messages_customer ON messages(customer_id);
CREATE INDEX IF NOT EXISTS idx_notifications_role ON notifications(role, done);

CREATE TABLE IF NOT EXISTS attention_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, -- packing_late | ready_not_picked | delivery_late
    level INTEGER NOT NULL DEFAULT 1, -- 1 = nudged staffer, 2 = owner looped in
    status TEXT NOT NULL DEFAULT 'open', -- open | resolved
    opened_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_attention_open ON attention_items(order_id, kind, status);
"""

# Tables wiped by reset_demo.py (transactional/demo data, not masters).
DEMO_TABLES = ("notifications", "messages", "events", "order_lines",
               "attention_items", "orders")

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
    "attention_items",
)


def _ensure_order_columns(conn) -> None:
    """ADD COLUMNs for DBs created before they existed (no-op otherwise)."""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='orders'"
    ).fetchone()
    if not exists:
        return  # fresh DB: SCHEMA creates the columns directly
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
    for col in ("track_token", "stage_entered_at", "eta_at"):
        if col not in cols:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")


def migrate(conn) -> None:
    """Idempotent upgrade for DBs created before a column/table existed."""
    _ensure_order_columns(conn)
    tables = {r["name"] for r in
              conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "attention_items" not in tables:
        conn.executescript(SCHEMA)
    # Backfill: every order gets a token; stage clock starts at creation.
    import secrets

    for r in conn.execute(
            "SELECT id, created_at FROM orders WHERE track_token IS NULL"):
        conn.execute(
            "UPDATE orders SET track_token = ?, stage_entered_at = ?"
            " WHERE id = ?",
            (secrets.token_urlsafe(16), r["created_at"], r["id"]))
    conn.commit()


def init_db(db_path: str | Path | None = None) -> Path:
    """Create all tables/indexes. Idempotent. Returns db path."""
    path = resolve_db_path(db_path)
    conn = get_conn(path)
    try:
        _ensure_order_columns(conn)
        conn.executescript(SCHEMA)
        migrate(conn)
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
