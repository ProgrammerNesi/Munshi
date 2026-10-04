"""Task boards, warmup, home page and the pipeline-failure fallback."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import engine  # noqa: E402
from app import warmup  # noqa: E402
from app.ai import llm  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.main import app  # noqa: E402
from app.rules import AUTO_CONFIRM, Decision  # noqa: E402
from scripts.seed import seed  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Seeded tmp DB; app resolves it via MUNSHI_DB per call."""
    db = tmp_path / "tasks.db"
    seed(db)
    monkeypatch.setenv("MUNSHI_DB", str(db))
    monkeypatch.setenv("MUNSHI_MOCK_AI", "1")
    llm.set_mock_json({"lines": [], "notes": ""})
    return TestClient(app), str(db)


def _packing_order(db):
    """Order driven to PACKING through the real engine (no mocks needed)."""
    oid = engine.create_order_from_draft(
        1, [{"item_id": 1, "qty": 2}], "do kilo cheeni", db_path=db)
    engine.apply_decision(oid, Decision(AUTO_CONFIRM, ["test"]), db_path=db)
    engine.assign_staff(oid, db_path=db)
    return oid


def test_home_page_links_roles(client):
    client, _db = client
    r = client.get("/")
    assert r.status_code == 200
    for href in ("/customer/1", "/owner", "/packer", "/delivery"):
        assert href in r.text


def test_health_has_warmup_key(client):
    client, _db = client
    assert set(client.get("/api/health").json()["warmup"]) == {"stt", "llm"}


def test_pack_flow_and_mismatch_lock(client):
    client, db = client
    oid = _packing_order(db)
    tasks = client.get("/api/tasks/packing").json()
    assert [t["id"] for t in tasks] == [oid]
    assert tasks[0]["lines"][0]["name"] == "Sugar"
    # Short pack locks dispatch and notifies the owner.
    r = client.post(f"/api/tasks/packing/{oid}", json={"counts": {"1": 1.0}})
    assert r.json()["status"] == "PACK_MISMATCH"
    assert client.get("/api/tasks/packing").json() == []
    bad = client.post("/api/tasks/packing/99999", json={"counts": {"1": 1.0}})
    assert bad.status_code == 409
    bad2 = client.post(f"/api/tasks/packing/{oid}", json={"counts": {"x": 1}})
    assert bad2.status_code == 400


def test_delivery_flow_and_problem(client):
    client, db = client
    oid = _packing_order(db)
    client.post(f"/api/tasks/packing/{oid}", json={"counts": {"1": 2.0}})
    ready = client.get("/api/tasks/delivery").json()
    assert [t["id"] for t in ready] == [oid]
    assert client.post(f"/api/tasks/delivery/{oid}/start").json() == {
        "status": "OUT_FOR_DELIVERY"}
    assert client.post(f"/api/tasks/delivery/{oid}/delivered",
                       json={"payment_mode": "cash"}).json() == {
                           "status": "DELIVERED"}
    # Problem path on a fresh order: back to READY + owner notified.
    oid2 = _packing_order(db)
    client.post(f"/api/tasks/packing/{oid2}", json={"counts": {"1": 2.0}})
    client.post(f"/api/tasks/delivery/{oid2}/start")
    r = client.post(f"/api/tasks/delivery/{oid2}/problem",
                    json={"note": "dukaan band"})
    assert r.json()["status"] == "READY_FOR_DELIVERY"
    assert client.post(f"/api/tasks/delivery/{oid2}/problem",
                       json={"note": ""}).status_code == 400
    assert client.post(f"/api/tasks/delivery/{oid2}/delivered",
                       json={"payment_mode": "barter"}).status_code == 409


def test_task_pages_serve(client):
    client, _db = client
    for path in ("/packer", "/delivery"):
        r = client.get(path)
        assert r.status_code == 200 and "tasks.js" in r.text


def test_warmup_marks_ready_in_mock(monkeypatch):
    import asyncio

    monkeypatch.setenv("MUNSHI_MOCK_AI", "1")
    warmup.STATUS.update({"stt": "warming", "llm": "warming"})
    asyncio.run(warmup.run())
    assert warmup.get_status() == {"stt": "ready", "llm": "ready"}


def test_warmup_marks_failed(monkeypatch):
    import asyncio

    monkeypatch.setattr("app.ai.stt.transcribe",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no mic")))
    warmup.STATUS.update({"stt": "warming", "llm": "warming"})
    asyncio.run(warmup.run())
    assert warmup.get_status()["stt"] == "failed"
    warmup.STATUS.update({"stt": "warming", "llm": "warming"})  # leave clean


def test_pipeline_crash_still_replies(monkeypatch, tmp_path):
    """Outer except: error logged AND customer gets could_not_understand."""
    from app import pipeline

    db = tmp_path / "crash.db"
    seed(db)
    monkeypatch.setenv("MUNSHI_DB", str(db))
    monkeypatch.setenv("MUNSHI_MOCK_AI", "1")
    conn = get_conn(str(db))
    try:
        mid = conn.execute(
            "INSERT INTO messages (customer_id, order_id, direction, text,"
            " audio_path, ts) VALUES (1, NULL, 'in', 'x', NULL, 't')").lastrowid
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(pipeline, "_fresh_order",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pipeline.process_message_now(mid, str(db)) is None
    conn = get_conn(str(db))
    try:
        out = conn.execute(
            "SELECT text FROM messages WHERE direction = 'out'").fetchall()
        err = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'error'").fetchone()[0]
    finally:
        conn.close()
    assert err == 1 and any("couldn't read" in r[0].lower() for r in out)
