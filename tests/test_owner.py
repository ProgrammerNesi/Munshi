"""Owner portal tests: board, actions, detail timeline, khata, stock, rules."""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import engine, owner as owner_mod  # noqa: E402
from app import pipeline  # noqa: E402
from app.ai import llm  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.main import app  # noqa: E402
from app.rules import AUTO_CONFIRM, Decision  # noqa: E402
from scripts.seed import seed  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Seeded tmp DB; all routes resolve it via MUNSHI_DB per call."""
    db = tmp_path / "owner.db"
    seed(db)
    monkeypatch.setenv("MUNSHI_DB", str(db))
    monkeypatch.setenv("MUNSHI_MOCK_AI", "1")
    llm.set_mock_json({"lines": [], "notes": ""})
    return TestClient(app), str(db)


def _say(customer_id, text, db):
    """Drop an inbound customer message straight into the DB."""
    conn = get_conn(db)
    try:
        cur = conn.execute(
            "INSERT INTO messages (customer_id, order_id, direction, text,"
            " audio_path, ts) VALUES (?,?, 'in', ?, NULL, ?)",
            (customer_id, None, text, "2026-01-01T00:00:00+00:00"))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _big_approval(db):
    """Credit-busting order for customer 4, processed to AWAITING_APPROVAL."""
    llm.set_mock_json({"lines": [
        {"raw": "300 basmati", "item_guess": "basmati", "qty": 300,
         "unit": "kg"},
        {"raw": "100 cheeni", "item_guess": "cheeni", "qty": 100,
         "unit": "kg"}], "notes": ""})
    return pipeline.process_message_now(_say(4, "bada order", db), db)


def _mismatch(db):
    """PACK_MISMATCH order via the deterministic engine path (no mocks)."""
    oid = engine.create_order_from_draft(
        1, [{"item_id": 1, "qty": 2}], "do kilo cheeni", db_path=db)
    engine.apply_decision(oid, Decision(AUTO_CONFIRM, ["test"]), db_path=db)
    engine.assign_staff(oid, db_path=db)
    engine.submit_pack_counts(oid, {1: 1.0}, db_path=db)
    return oid


def test_board_groups_and_needs_you(client):
    client, db = client
    aid, mid = _big_approval(db), _mismatch(db)
    board = client.get("/api/owner/board").json()
    assert {o["id"] for o in board["needs_you"]} == {aid, mid}
    card = next(o for o in board["needs_you"] if o["id"] == aid)
    assert any("credit limit" in r for r in card["reasons"])  # rulebook first
    assert "AWAITING_APPROVAL" in board["groups"]
    assert board["groups"]["AWAITING_APPROVAL"][0]["customer"] == "Patel Traders"


def test_owner_board_hides_seeded_history_but_keeps_shop_orders(client):
    client, db = client
    real_order = _mismatch(db)
    board = client.get("/api/owner/board").json()
    visible = [
        order
        for orders in board["groups"].values()
        for order in orders
    ]
    assert visible
    assert all("[DEMO HISTORY]" not in order["customer"] for order in visible)
    assert any(order["id"] == real_order for order in visible)
    assert board["summary"]["orders"] == 1
    assert board["summary"]["active"] == 1
    assert board["summary"]["needs_you"] == 1


def test_staff_task_pages_use_english_labels(client):
    client, _db = client
    packer = client.get("/packer")
    delivery = client.get("/delivery")
    assert packer.status_code == delivery.status_code == 200
    assert "Packing orders" in packer.text
    assert "Can't connect to the shop" in packer.text
    assert "Delivery orders" in delivery.text
    assert "Can't connect to the shop" in delivery.text


def _new_customer_hold(db):
    """Small order from the new customer: held for approval, sips no stock."""
    llm.set_mock_json({"lines": [
        {"raw": "do kilo cheeni", "item_guess": "cheeni", "qty": 2,
         "unit": "kg"}], "notes": ""})
    return pipeline.process_message_now(_say(10, "do kilo cheeni", db), db)


def test_approve_and_decline(client):
    client, db = client
    aid = _big_approval(db)
    assert client.post(f"/api/owner/order/{aid}/approve").json()["status"] \
        == "PACKING"  # approved then handed to the packer
    assert client.post(f"/api/owner/order/{aid}/approve").status_code == 409
    bid = _new_customer_hold(db)
    r = client.post(f"/api/owner/order/{bid}/decline",
                    json={"reason": "limit khatm"})
    assert r.json()["status"] == "CANCELLED"
    conn = get_conn(db)
    try:
        assert conn.execute(
            "SELECT status FROM orders WHERE id = ?", (bid,)).fetchone()[0] \
            == "CANCELLED"
    finally:
        conn.close()


def test_mismatch_resolve_paths(client):
    client, db = client
    assert client.post(f"/api/owner/order/{_mismatch(db)}/mismatch",
                       json={"mode": "accept_partial"}).json()["status"] \
        == "READY_FOR_DELIVERY"
    assert client.post(f"/api/owner/order/{_mismatch(db)}/mismatch",
                       json={"mode": "recount"}).json()["status"] == "PACKING"
    assert client.post(f"/api/owner/order/{_mismatch(db)}/mismatch",
                       json={"mode": "bogus"}).status_code == 409


def test_order_detail_timeline_with_agent_rows(client):
    client, db = client
    from app import agent as agent_mod

    mid = _mismatch(db)
    agent_mod.run_agent(mid, db_path=db)  # mock LLM -> fallback row, real code
    d = client.get(f"/api/owner/order/{mid}").json()
    assert d["transcript"] == "do kilo cheeni" and d["has_agent"] is True
    assert d["bill"]["total"] > 0 and len(d["lines"]) == 1
    kinds = [e["kind"] for e in d["timeline"]]
    assert "mismatch" in kinds and "agent_fallback" in kinds
    fb = next(e for e in d["timeline"] if e["kind"] == "agent_fallback")
    assert fb["seconds"] is not None and "fallback" in fb["message"].lower()
    assert client.get("/api/owner/order/99999").status_code == 404


def test_khata_math(client):
    client, _db = client
    rows = {c["name"]: c for c in client.get("/api/owner/khata").json()}
    patel = rows["Patel Traders"]
    assert (patel["outstanding"], patel["limit"], patel["pct"]) == (18000, 50000, 36)
    assert patel["last_order"] != ""  # seeded history counts
    assert all(set(c) >= {"outstanding", "limit", "pct", "last_order"}
               for c in rows.values())


def test_stock_days_math(client):
    client, db = client
    conn = get_conn(db)  # force one item under its reorder level
    try:
        conn.execute("UPDATE items SET stock_qty = 1 WHERE id = 30")
        conn.commit()
    finally:
        conn.close()
    rows = {it["name"]: it for it in client.get("/api/owner/stock").json()}
    sugar = rows["Sugar"]
    assert sugar["days_left"] is not None and sugar["days_left"] > 0
    assert set(sugar) >= {"stock", "used_per_day", "days_left", "low"}
    assert rows["Garam Masala"]["low"] is True


def test_rules_get_save_validate(client, tmp_path, monkeypatch):
    client, db = client
    assert "auto_confirm_max_amount" in client.get("/api/owner/rules").json()["text"]
    # Point the editor at a scratch copy: the repo file is never touched here.
    scratch = tmp_path / "rules.yaml"
    scratch.write_text(owner_mod.rules_text(), encoding="utf-8")
    monkeypatch.setattr(owner_mod, "RULES_PATH", scratch)
    good = scratch.read_text(encoding="utf-8").replace("fee: 40", "fee: 50")
    assert client.post("/api/owner/rules", json={"text": good}).json() == {
        "saved": True}
    assert "fee: 50" in scratch.read_text(encoding="utf-8")
    conn = get_conn(db)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind = 'rules_updated'"
        ).fetchone()["n"] == 1
    finally:
        conn.close()
    bad = good.replace("fee: 50", "fee: lots")
    assert client.post("/api/owner/rules", json={"text": bad}).status_code == 400
    assert "fee: 50" in scratch.read_text(encoding="utf-8")  # untouched
    missing = good.replace("  fee: 50\n", "")
    assert client.post("/api/owner/rules",
                       json={"text": missing}).status_code == 400
    assert client.post("/api/owner/rules",
                       json={"text": "not: [valid"}).status_code == 400


def test_notifications_feed_done_and_bell(client):
    client, db = client
    _big_approval(db)  # raises an owner notification
    feed = client.get("/api/owner/notifications").json()
    assert feed["unread"] >= 1 and feed["items"]
    state = client.get("/api/state").json()  # owner mode: no customer_id
    assert set(state) >= {"orders_by_status", "needs_you", "notifications",
                          "queue_depth"}
    assert state["needs_you"] >= 1 and state["notifications"] >= 1
    nid = feed["items"][0]["id"]
    assert client.post(f"/api/owner/notifications/{nid}/done").json() == {
        "done": True}
    assert client.get("/api/owner/notifications").json()["unread"] == \
        feed["unread"] - 1
    assert client.post("/api/owner/notifications/99999/done").status_code == 404


def test_owner_page_and_health_extra(client):
    client, _db = client
    page = client.get("/owner")
    assert page.status_code == 200 and "Needs you" not in page.text
    assert "Munshi · Owner" in page.text and "rules-text" in page.text
    extra = client.get("/api/owner/health_extra").json()
    assert set(extra["stt"]) == {"backend", "model"}
    assert set(extra["last_order"]) == {"order_id", "stages"}
