"""End-to-end tests for the order state machine (seeded tmp DB)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import engine  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.rules import ASK_OWNER, AUTO_CONFIRM, evaluate  # noqa: E402
from scripts.seed import seed  # noqa: E402


@pytest.fixture
def db(tmp_path):
    """Fresh seeded DB per test."""
    path = tmp_path / "phase2.db"
    seed(path)
    return path


def _ctx(db, customer_id):
    """Build (customer dict, stock dict) for rules.evaluate from live rows."""
    conn = get_conn(db)
    try:
        c = conn.execute(
            "SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        customer = {
            "name": c["name"], "credit_limit": c["credit_limit"],
            "outstanding": c["outstanding"], "is_new": c["is_new"],
            "usual_basket": json.loads(c["usual_basket_json"]),
        }
        stock = {r["id"]: {"name": r["name"], "price": r["price"],
                           "stock_qty": r["stock_qty"]}
                 for r in conn.execute("SELECT * FROM items")}
        return customer, stock
    finally:
        conn.close()


def _status(db, order_id):
    conn = get_conn(db)
    try:
        return conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()["status"]
    finally:
        conn.close()


def _run_to_door(db, order_id, payment="cash", counts=None):
    """Pack (full counts by default), dispatch, deliver. Returns order row."""
    packer_counts = counts or {1: 2.0}
    engine.assign_staff(order_id, db)
    engine.submit_pack_counts(order_id, packer_counts, db_path=db)
    engine.start_delivery(order_id, db_path=db)
    engine.mark_delivered(order_id, payment, db_path=db)
    conn = get_conn(db)
    try:
        return conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    finally:
        conn.close()


# -- gate 1: routine auto -------------------------------------------------

def test_routine_order_auto_confirms_and_delivers(db):
    customer, stock = _ctx(db, 1)  # Ramesh: limit 20000, due 4500
    assert evaluate({"lines": [{"item_id": 1, "qty": 2}]},
                    customer, stock).action == AUTO_CONFIRM

    oid = engine.create_order_from_draft(1, [{"item_id": 1, "qty": 2}],
                                        "2kg cheeni", db_path=db)
    assert engine.apply_decision(
        oid, evaluate({"lines": [{"item_id": 1, "qty": 2}]}, customer, stock),
        db_path=db) == engine.CONFIRMED
    # Stock reserved at CONFIRMED: 500 -> 498.
    conn = get_conn(db)
    try:
        assert conn.execute(
            "SELECT stock_qty FROM items WHERE id = 1").fetchone()[0] == 498
    finally:
        conn.close()

    row = _run_to_door(db, oid)
    assert row["status"] == engine.DELIVERED
    assert row["total"] == 2 * 45.0 + 40  # subtotal + delivery fee (< 2000)
    # Cash sale leaves the khata alone.
    conn = get_conn(db)
    try:
        assert conn.execute(
            "SELECT outstanding FROM customers WHERE id = 1").fetchone()[0] == 4500
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE order_id = ?", (oid,)
        ).fetchone()[0] >= 6  # every step logged
    finally:
        conn.close()


# -- gate 2: credit over limit --------------------------------------------

def test_credit_over_limit_needs_owner_then_updates_khata(db):
    # Patel Traders: limit 50000, outstanding 18000. This draft tops 32000.
    draft = {"lines": [{"item_id": 3, "qty": 300}, {"item_id": 1, "qty": 100}]}
    customer, stock = _ctx(db, 4)
    decision = evaluate(draft, customer, stock)
    assert decision.action == ASK_OWNER
    assert any("credit limit" in r for r in decision.reasons)

    oid = engine.create_order_from_draft(4, draft["lines"], "bada order",
                                         db_path=db)
    assert engine.apply_decision(oid, decision, db_path=db) == \
        engine.NEEDS_OWNER_APPROVAL
    assert engine.owner_approve(oid, 1, db_path=db) == engine.CONFIRMED

    conn = get_conn(db)
    try:
        total = conn.execute(
            "SELECT total FROM orders WHERE id = ?", (oid,)).fetchone()[0]
    finally:
        conn.close()
    engine.assign_staff(oid, db)
    engine.submit_pack_counts(oid, {3: 300.0, 1: 100.0}, db_path=db)
    engine.start_delivery(oid, db_path=db)
    engine.mark_delivered(oid, "credit", db_path=db)

    conn = get_conn(db)
    try:
        assert _status(db, oid) == engine.DELIVERED
        assert conn.execute(
            "SELECT outstanding FROM customers WHERE id = 4").fetchone()[0] \
            == pytest.approx(18000 + total)
    finally:
        conn.close()


def test_owner_can_decline_instead(db):
    customer, stock = _ctx(db, 4)
    draft = {"lines": [{"item_id": 3, "qty": 300}, {"item_id": 1, "qty": 100}]}
    oid = engine.create_order_from_draft(4, draft["lines"], db_path=db)
    engine.apply_decision(oid, evaluate(draft, customer, stock), db_path=db)
    assert engine.owner_decline(oid, 1, "limit khatm", db_path=db) == \
        engine.CANCELLED


# -- gate 3: pack mismatch lock --------------------------------------------

def test_pack_mismatch_locks_dispatch_until_owner_resolves(db):
    oid = engine.create_order_from_draft(1, [{"item_id": 1, "qty": 10}],
                                         db_path=db)
    customer, stock = _ctx(db, 1)
    engine.apply_decision(oid, evaluate(
        {"lines": [{"item_id": 1, "qty": 10}]}, customer, stock), db_path=db)
    engine.assign_staff(oid, db)

    # Packed 6 of 10 with tolerance 0 -> locked.
    assert engine.submit_pack_counts(oid, {1: 6.0}, db_path=db) == \
        engine.PACK_MISMATCH
    with pytest.raises(engine.InvalidTransition):
        engine.start_delivery(oid, db_path=db)  # dispatch locked

    conn = get_conn(db)
    try:
        notes = conn.execute(
            "SELECT role, staff_id, action_required FROM notifications"
            " WHERE order_id = ?", (oid,)).fetchall()
        roles = {(n["role"], n["action_required"]) for n in notes}
        assert ("owner", 1) in roles and ("packer", 1) in roles
    finally:
        conn.close()

    # Owner accepts the partial: rebilled on 6kg, 4kg back on shelf.
    assert engine.owner_resolve_mismatch(oid, "accept_partial", 1,
                                        db_path=db) == engine.READY_FOR_DELIVERY
    conn = get_conn(db)
    try:
        row = conn.execute(
            "SELECT total FROM orders WHERE id = ?", (oid,)).fetchone()
        assert row[0] == 6 * 45.0 + 40  # packed subtotal + fee, recomputed
        assert conn.execute(
            "SELECT stock_qty FROM items WHERE id = 1").fetchone()[0] == 494
    finally:
        conn.close()
    engine.start_delivery(oid, db_path=db)
    engine.mark_delivered(oid, "upi", db_path=db)
    assert _status(db, oid) == engine.DELIVERED


def test_mismatch_recount_and_cancel_paths(db):
    oid = engine.create_order_from_draft(1, [{"item_id": 1, "qty": 10}],
                                         db_path=db)
    customer, stock = _ctx(db, 1)
    engine.apply_decision(oid, evaluate(
        {"lines": [{"item_id": 1, "qty": 10}]}, customer, stock), db_path=db)
    engine.assign_staff(oid, db)
    engine.submit_pack_counts(oid, {1: 9.0}, db_path=db)
    assert engine.owner_resolve_mismatch(oid, "recount", 1,
                                         db_path=db) == engine.PACKING
    # Packer gets it right the second time.
    assert engine.submit_pack_counts(oid, {1: 10.0}, db_path=db) == \
        engine.READY_FOR_DELIVERY

    oid2 = engine.create_order_from_draft(1, [{"item_id": 1, "qty": 10}],
                                          db_path=db)
    engine.apply_decision(oid2, evaluate(
        {"lines": [{"item_id": 1, "qty": 10}]}, customer, stock), db_path=db)
    engine.assign_staff(oid2, db)
    engine.submit_pack_counts(oid2, {1: 2.0}, db_path=db)
    assert engine.owner_resolve_mismatch(oid2, "cancel", 1,
                                         db_path=db) == engine.CANCELLED


# -- invalid transitions + cancel/release ----------------------------------

def test_invalid_transitions_rejected(db):
    oid = engine.create_order_from_draft(1, [{"item_id": 1, "qty": 2}],
                                         db_path=db)
    with pytest.raises(engine.InvalidTransition):
        engine.mark_delivered(oid, "cash", db_path=db)  # NEW -> DELIVERED
    with pytest.raises(engine.InvalidTransition):
        engine.submit_pack_counts(oid, {1: 2.0}, db_path=db)  # NEW, not PACKING
    with pytest.raises(engine.InvalidTransition):
        engine.owner_approve(oid, 1, db_path=db)  # nothing holding it

    customer, stock = _ctx(db, 1)
    engine.apply_decision(oid, evaluate(
        {"lines": [{"item_id": 1, "qty": 2}]}, customer, stock), db_path=db)
    with pytest.raises(engine.InvalidTransition):
        engine.apply_decision(oid, evaluate(
            {"lines": [{"item_id": 1, "qty": 2}]}, customer, stock),
            db_path=db)  # decision twice


def test_cancel_releases_reserved_stock(db):
    oid = engine.create_order_from_draft(1, [{"item_id": 1, "qty": 2}],
                                         db_path=db)
    customer, stock = _ctx(db, 1)
    engine.apply_decision(oid, evaluate(
        {"lines": [{"item_id": 1, "qty": 2}]}, customer, stock), db_path=db)
    assert engine.cancel_order(oid, "owner", "customer called off",
                               db_path=db) == engine.CANCELLED
    conn = get_conn(db)
    try:
        assert conn.execute(
            "SELECT stock_qty FROM items WHERE id = 1").fetchone()[0] == 500
    finally:
        conn.close()


def test_delivery_problem_returns_order_to_ready(db):
    oid = engine.create_order_from_draft(1, [{"item_id": 1, "qty": 2}],
                                         db_path=db)
    customer, stock = _ctx(db, 1)
    engine.apply_decision(oid, evaluate(
        {"lines": [{"item_id": 1, "qty": 2}]}, customer, stock), db_path=db)
    engine.assign_staff(oid, db)
    engine.submit_pack_counts(oid, {1: 2.0}, db_path=db)
    engine.start_delivery(oid, db_path=db)
    assert engine.report_delivery_problem(oid, "shop locked", db_path=db) == \
        engine.READY_FOR_DELIVERY
