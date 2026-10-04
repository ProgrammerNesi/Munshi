"""Tests for scripts/reset_demo.py — wipe demo tables, restore snapshots."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import get_conn  # noqa: E402
from scripts.reset_demo import reset_demo  # noqa: E402
from scripts.seed import seed  # noqa: E402


def test_reset_wipes_demo_and_restores_snapshots(tmp_path):
    db = tmp_path / "reset.db"
    seed(db)

    conn = get_conn(db)
    try:
        # simulate live activity: a new order + drifted stock/outstanding
        conn.execute(
            "INSERT INTO orders (customer_id, status, transcript, total,"
            " payment_mode, created_at, updated_at)"
            " VALUES (1, 'new', 'live order', 100, 'cash', 'x', 'x')"
        )
        conn.execute("UPDATE items SET stock_qty = 1 WHERE id = 1")
        conn.execute("UPDATE customers SET outstanding = 99999 WHERE id = 1")
        conn.commit()
    finally:
        conn.close()

    counts = reset_demo(db)
    assert counts["orders"] == 0
    assert counts["order_lines"] == 0
    assert counts["events"] == 0
    assert counts["messages"] == 0
    assert counts["notifications"] == 0

    conn = get_conn(db)
    try:
        # masters untouched, snapshots restored
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 30
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM customers").fetchone()["n"] == 10
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM staff").fetchone()["n"] == 5
        assert conn.execute(
            "SELECT stock_qty AS q FROM items WHERE id = 1").fetchone()["q"] == 500
        assert conn.execute(
            "SELECT outstanding AS o FROM customers WHERE id = 1"
        ).fetchone()["o"] == 4500
    finally:
        conn.close()


def test_reset_on_fresh_db_is_safe(tmp_path):
    db = tmp_path / "fresh.db"
    counts = reset_demo(db)  # no seed first: tables created, nothing to wipe
    assert counts["orders"] == 0
    assert counts["items"] == 0  # masters only come from seed.py
