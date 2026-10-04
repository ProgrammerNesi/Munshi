"""Phase 8: demo scenes primed deterministically (mock AI, exact payloads)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai import llm  # noqa: E402
from app.db import get_conn  # noqa: E402
from scripts import demo_setup  # noqa: E402
from scripts.seed import seed  # noqa: E402


def _db(tmp_path, monkeypatch):
    path = tmp_path / "demo.db"
    seed(path)
    monkeypatch.setenv("MUNSHI_DB", str(path))
    monkeypatch.setenv("MUNSHI_MOCK_AI", "1")
    return str(path)


def _lines(*triples):
    return {"lines": [{"raw": r, "item_guess": g, "qty": q, "unit": u}
                      for r, g, q, u in triples], "notes": ""}


def _status(db, oid):
    conn = get_conn(db)
    try:
        return conn.execute(
            "SELECT status FROM orders WHERE id = ?", (oid,)).fetchone()[0]
    finally:
        conn.close()


def test_scene_a_auto_confirms(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    llm.set_mock_json(_lines(("do kilo cheeni", "cheeni", 2, "kg"),
                             ("paanch kilo atta", "atta", 5, "kg")))
    assert _status(db, demo_setup.scene_a(db)) == "PACKING"  # auto + packer


def test_scene_b_hits_credit_limit(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    conn = get_conn(db)
    try:
        original_outstanding = conn.execute(
            "SELECT outstanding FROM customers WHERE id = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    llm.set_mock_json(_lines(("50 kilo cheeni", "cheeni", 50, "kg")))
    oid = demo_setup.scene_b(db)
    assert _status(db, oid) == "AWAITING_APPROVAL"
    conn = get_conn(db)
    try:
        out = conn.execute(
            "SELECT outstanding FROM customers WHERE id = 1"
        ).fetchone()[0]
        assert out == original_outstanding
    finally:
        conn.close()


def test_demo_scenes_share_one_customer_store(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    llm.set_mock_json(_lines(("do kilo cheeni", "cheeni", 2, "kg"),
                             ("paanch kilo atta", "atta", 5, "kg")))
    demo_setup.scene_a(db)
    llm.set_mock_json(_lines(("50 kilo cheeni", "cheeni", 50, "kg")))
    demo_setup.scene_b(db)
    llm.set_mock_json(_lines(("2 kilo haldi", "haldi", 2, "kg")))
    demo_setup.scene_c(db)
    demo_setup.scene_d(db)

    conn = get_conn(db)
    try:
        customer_ids = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT customer_id FROM orders"
                " WHERE COALESCE(transcript, '') NOT LIKE '[DEMO HISTORY]%'"
            )
        }
    finally:
        conn.close()
    assert customer_ids == {demo_setup.DEMO_CUSTOMER_ID}


def test_scene_c_mismatch_lock(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    llm.set_mock_json(_lines(("2 kilo haldi", "haldi", 2, "kg")))
    assert _status(db, demo_setup.scene_c(db)) == "PACK_MISMATCH"


def test_scene_d_starts_packing_for_live_tracking_demo(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    oid = demo_setup.scene_d(db)
    conn = get_conn(db)
    try:
        row = conn.execute(
            "SELECT status, track_token, transcript FROM orders WHERE id = ?",
            (oid,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "PACKING"
    assert row["track_token"] and len(row["track_token"]) >= 16
    assert "[DEMO SCENE D]" in row["transcript"]


def test_demo_main_marks_scenes(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path, monkeypatch)
    llm.set_mock_json(_lines(("x", "cheeni", 1, "kg")))
    monkeypatch.setattr(demo_setup, "reset_demo", lambda db_path=None: {})
    import sqlite3
    conn = get_conn(db)
    try:  # minimal orders so status lookup works
        ids = []
        for st in ("PACKING", "AWAITING_APPROVAL", "PACK_MISMATCH", "PACKING"):
            cur = conn.execute(
                "INSERT INTO orders (customer_id, status, transcript, total,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (1, st, "", 0, "2026-01-01", "2026-01-01"))
            ids.append(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(demo_setup, "scene_a", lambda db_path=None: ids[0])
    monkeypatch.setattr(demo_setup, "scene_b", lambda db_path=None: ids[1])
    monkeypatch.setattr(demo_setup, "scene_c", lambda db_path=None: ids[2])
    monkeypatch.setattr(demo_setup, "scene_d", lambda db_path=None: ids[3])
    monkeypatch.setattr(sys, "argv", ["demo_setup.py", "--db", db])
    demo_setup.main()
    out = capsys.readouterr().out
    assert out.count("PASS") == 4 and "Scene A" in out and "Scene D" in out
