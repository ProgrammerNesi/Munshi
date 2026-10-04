"""Supervisor tests: fake demo clock, real engine, tmp DBs."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import clock  # noqa: E402
from app import engine  # noqa: E402
from app import supervisor  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.main import app  # noqa: E402
from app.rules import AUTO_CONFIRM, Decision, supervision_minutes  # noqa: E402
from scripts.seed import seed  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Seeded tmp DB, demo mode on, clock zeroed (fake clock per test)."""
    path = tmp_path / "super.db"
    seed(path)
    monkeypatch.setenv("MUNSHI_DB", str(path))
    monkeypatch.setenv("MUNSHI_DEMO", "1")
    monkeypatch.delenv("MUNSHI_MOCK_AI", raising=False)
    clock.reset()
    yield str(path)
    clock.reset()


def _packing_order(db):
    """Fresh PACKING order for customer 1 (2kg sugar)."""
    oid = engine.create_order_from_draft(
        1, [{"item_id": 1, "qty": 2}], "do kilo cheeni", db_path=db)
    engine.apply_decision(oid, Decision(AUTO_CONFIRM, ["t"]), db_path=db)
    engine.assign_staff(oid, db_path=db)
    return oid


def _row(db, sql, args=()):
    conn = get_conn(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_l1_then_l2_with_dedupe(db):
    oid = _packing_order(db)
    assert supervisor.scan_once(db)["l1"] == []  # fresh: not late yet
    clock.skip(2)  # demo packing SLA is 20//10 = 2 min
    first = supervisor.scan_once(db)
    assert first["l1"] == [oid] and first["l2"] == []
    # L1: packer notified, no customer message yet.
    notes = _row(db, "SELECT role, staff_id FROM notifications WHERE order_id = ?",
                 (oid,))
    assert ("packer", 2) in [(r[0], r[1]) for r in notes]
    assert tuple(_row(db, "SELECT level, status FROM attention_items WHERE order_id = ?",
                (oid,))[0]) == (1, "open")
    n_out = len(_row(db, "SELECT id FROM messages WHERE direction = 'out'"))
    # Same lateness, second scan: nothing new (dedupe).
    again = supervisor.scan_once(db)
    assert again["l1"] == [] and again["l2"] == []
    assert len(_row(db, "SELECT id FROM messages WHERE direction = 'out'")) == n_out
    # Past sla + gap (2 + 1): L2 fires once.
    clock.skip(1)
    second = supervisor.scan_once(db)
    assert second["l2"] == [oid]
    assert supervisor.scan_once(db)["l2"] == []  # no repeat customer message
    out = _row(db, "SELECT text FROM messages WHERE direction = 'out'"
                   " AND order_id = ?", (oid,))
    assert any("/t/" in r[0] for r in out)  # tracking link, sent once
    assert any("lagbhag" in r[0] for r in out)


def test_l2_reassigns_to_other_packer(db):
    oid = _packing_order(db)
    conn = get_conn(db)
    try:
        first = conn.execute(
            "SELECT packer_id FROM orders WHERE id = ?", (oid,)).fetchone()[0]
    finally:
        conn.close()
    assert first == 2  # least-busy tie broken by lowest id
    clock.skip(3)
    supervisor.scan_once(db)
    conn = get_conn(db)
    try:
        second = conn.execute(
            "SELECT packer_id FROM orders WHERE id = ?", (oid,)).fetchone()[0]
        owner_note = conn.execute(
            "SELECT text FROM notifications WHERE order_id = ? AND role = 'owner'"
            " ORDER BY id DESC LIMIT 1", (oid,)).fetchone()[0]
    finally:
        conn.close()
    assert second == 3 and second != first
    assert "Imran Packer" in owner_note


def test_auto_resolve_when_stage_moves(db):
    oid = _packing_order(db)
    clock.skip(2)
    supervisor.scan_once(db)
    assert _row(db, "SELECT status FROM attention_items WHERE order_id = ?",
                (oid,))[0][0] == "open"
    engine.submit_pack_counts(oid, {1: 2.0}, db_path=db)  # -> READY
    done = supervisor.scan_once(db)
    assert done["resolved"] == [oid]
    assert tuple(_row(db, "SELECT status FROM attention_items WHERE order_id = ?",
                (oid,))[0]) == ("resolved",)


def test_loop_survives_a_bad_order(db, monkeypatch):
    good = _packing_order(db)
    conn = get_conn(db)
    try:  # garbage timestamp: fromisoformat blows up for this order only
        conn.execute("UPDATE orders SET stage_entered_at = 'not-a-time'"
                     " WHERE id = ?", (good,))
        conn.commit()
    finally:
        conn.close()
    other = _packing_order(db)
    clock.skip(5)
    summary = supervisor.scan_once(db)
    assert summary["errors"] and summary["errors"][0]["order_id"] == good
    assert summary["l2"] == [other]  # the good order still escalated


def test_tick_track_and_skip_endpoints(db, monkeypatch):
    client = TestClient(app)
    assert set(client.post("/api/supervisor/tick").json()) >= \
        {"checked", "l1", "l2", "resolved", "errors"}
    oid = _packing_order(db)
    conn = get_conn(db)
    try:
        token = conn.execute(
            "SELECT track_token FROM orders WHERE id = ?", (oid,)).fetchone()[0]
    finally:
        conn.close()
    assert token and len(token) >= 16
    page = client.get(f"/t/{token}")
    assert page.status_code == 200 and "Pack ho raha hai" in page.text
    for secret in ("9800000001", "outstanding", "4500", "Ramesh"):
        assert secret not in page.text  # no ids, phones, names, balances
    assert client.get("/t/nope").status_code == 404
    assert client.post("/api/demo/skip?minutes=10").json() == {"offset_min": 10}
    assert client.get("/api/demo/clock").json() == {"demo": True, "offset_min": 10}
    monkeypatch.setenv("MUNSHI_DEMO", "0")
    assert client.post("/api/demo/skip?minutes=10").status_code == 400
    assert "demo.js" in client.get("/owner").text


def test_clock_and_sla_validation(monkeypatch):
    monkeypatch.setenv("MUNSHI_DEMO", "0")
    try:
        clock.skip(5)
        raised = False
    except ValueError:
        raised = True
    assert raised  # skip refused outside demo mode
    from app.rules import load_rules

    slas = supervision_minutes(load_rules(), demo=True)
    assert slas == {"packing_late": 2, "ready_wait": 1, "delivery_late": 4,
                    "escalation_gap": 1}  # /10, minimum 1
    for bad in ({"supervision": {}}, {"supervision": {"packing_late": -5,
                                                      "ready_wait": 1,
                                                      "delivery_late": 1,
                                                      "escalation_gap": 1}}):
        try:
            supervision_minutes(bad)
            valid = True
        except ValueError:
            valid = False
        assert not valid
