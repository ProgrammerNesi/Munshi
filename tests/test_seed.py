"""Tests for scripts/seed.py — masters + 30-day FAKE history."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import get_conn  # noqa: E402
from scripts.seed import seed  # noqa: E402


def test_seed_masters(tmp_path):
    db = tmp_path / "seed_master.db"
    counts = seed(db)
    assert counts["items"] == 30
    assert counts["customers"] == 10
    # Spec said "3 staff (1 owner, 2 packers, 2 delivery)" — that adds up
    # to 5 roles, so we seed 5 staff rows. See data/staff.csv.
    assert counts["staff"] == 5

    conn = get_conn(db)
    try:
        roles = [r["role"] for r in conn.execute("SELECT role FROM staff")]
        assert roles.count("owner") == 1
        assert roles.count("packer") == 2
        assert roles.count("delivery") == 2
        # aliases + baskets are valid JSON
        for r in conn.execute("SELECT aliases_json FROM items"):
            assert isinstance(json.loads(r["aliases_json"]), list)
        for r in conn.execute("SELECT usual_basket_json FROM customers"):
            assert isinstance(json.loads(r["usual_basket_json"]), list)
        # varied credit limits + exactly one new customer
        limits = [r["credit_limit"]
                  for r in conn.execute("SELECT credit_limit FROM customers")]
        assert len(set(limits)) >= 4
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM customers WHERE is_new = 1"
        ).fetchone()["n"] == 1
    finally:
        conn.close()


def test_single_shop_seed_has_one_customer_and_only_its_history(tmp_path):
    db = tmp_path / "single_shop.db"
    counts = seed(db, single_shop=True)
    assert counts["customers"] == 1
    assert counts["orders"] > 0

    conn = get_conn(db)
    try:
        customers = conn.execute(
            "SELECT id, name FROM customers"
        ).fetchall()
        order_customer_ids = {
            row["customer_id"]
            for row in conn.execute("SELECT DISTINCT customer_id FROM orders")
        }
        assert [(row["id"], row["name"]) for row in customers] == [
            (1, "Ramesh Kirana")
        ]
        assert order_customer_ids == {1}
    finally:
        conn.close()


def test_seed_fake_history(tmp_path):
    db = tmp_path / "seed_hist.db"
    counts = seed(db)
    assert counts["orders"] > 50  # ~9 customers x ~21 days
    assert counts["order_lines"] >= counts["orders"] * 2
    assert counts["events"] == counts["orders"]
    assert counts["messages"] == counts["orders"]

    conn = get_conn(db)
    try:
        # every seeded order is clearly labelled demo history
        bad = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE transcript NOT LIKE"
            " '[DEMO HISTORY]%'"
        ).fetchone()["n"]
        assert bad == 0
        bad_ev = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind != 'seed'"
        ).fetchone()["n"]
        assert bad_ev == 0
        for r in conn.execute("SELECT data_json FROM events"):
            assert json.loads(r["data_json"]).get("demo_history") is True
        # totals match lines; stock-usage stats exist per item
        for o in conn.execute("SELECT id, total FROM orders"):
            s = conn.execute(
                "SELECT COALESCE(SUM(qty * unit_price), 0) AS s"
                " FROM order_lines WHERE order_id = ?", (o["id"],)
            ).fetchone()["s"]
            assert abs(s - o["total"]) < 0.02
        assert conn.execute(
            "SELECT COUNT(DISTINCT item_id) AS n FROM order_lines"
        ).fetchone()["n"] >= 20
        # new customer has no history by definition
        new_id = conn.execute(
            "SELECT id FROM customers WHERE is_new = 1"
        ).fetchone()["id"]
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE customer_id = ?", (new_id,)
        ).fetchone()["n"] == 0
    finally:
        conn.close()


def test_seed_idempotent_and_stock_snapshot(tmp_path):
    db = tmp_path / "seed_idem.db"
    first = seed(db)
    второй = seed(db)
    assert first == второй

    conn = get_conn(db)
    try:
        # history does not consume the CSV stock snapshot
        row = conn.execute("SELECT stock_qty FROM items WHERE id = 1").fetchone()
        assert row["stock_qty"] == 500
    finally:
        conn.close()
