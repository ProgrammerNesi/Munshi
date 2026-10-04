"""Phase 4 end-to-end: TestClient + worker, MUNSHI_MOCK_AI=1, fake LLM JSON."""

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai import llm  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.main import app  # noqa: E402
from scripts.seed import seed  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Seeded tmp DB; app + worker resolve it via MUNSHI_DB per call."""
    db = tmp_path / "phase4.db"
    seed(db)
    monkeypatch.setenv("MUNSHI_DB", str(db))
    monkeypatch.setenv("MUNSHI_MOCK_AI", "1")
    llm.set_mock_json({"lines": [], "notes": ""})
    return TestClient(app), str(db)


def _line(raw, guess, qty, unit):
    return {"raw": raw, "item_guess": guess, "qty": qty, "unit": unit}


def _post(client, customer, text):
    r = client.post(f"/api/customer/{customer}/message", data={"text": text})
    assert r.status_code == 200, r.text
    assert "message_id" in r.json()  # immediate ack, processed in background
    return r.json()["message_id"]


def _wait_for(fn, timeout=15):
    """Poll until fn() is truthy (background worker needs a moment)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.2)
    return False


def _state(client, customer):
    return client.get("/api/state", params={"customer_id": customer}).json()


def _out_texts(client, customer):
    return [m["text"] for m in _state(client, customer)["messages"]
            if m["direction"] == "out"]


def _latest_status(client, customer):
    orders = _state(client, customer)["orders"]
    live = [o for o in orders if o["status"].isupper()]
    return max(live, key=lambda o: o["id"])["status"] if live else None


def test_normal_text_order_confirmed_with_bill(client):
    client, _db = client
    llm.set_mock_json(
        {"lines": [_line("do kilo cheeni", "cheeni", 2, "kg")], "notes": ""})
    _post(client, 1, "do kilo cheeni")
    # CONFIRMED then immediately PACKING (packer auto-assigned); bill sent.
    assert _wait_for(lambda: _latest_status(client, 1) == "PACKING")
    assert any("Total:" in t for t in _out_texts(client, 1))  # bill message
    s = _state(client, 1)
    assert s["orders"][0]["bill"]["total"] == 2 * 45.0 + 40  # fee under 2000


def test_credit_over_limit_waits_for_owner(client):
    client, db = client
    llm.set_mock_json({"lines": [_line("300 basmati", "basmati", 300, "kg"),
                                  _line("100 cheeni", "cheeni", 100, "kg")],
                       "notes": ""})
    _post(client, 4, "bada order")
    assert _wait_for(lambda: _latest_status(client, 4) == "AWAITING_APPROVAL")
    assert any("malik" in t for t in _out_texts(client, 4))
    conn = get_conn(db)  # owner got an action-required notification
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE role = 'owner'"
            " AND action_required = 1 AND done = 0").fetchone()["n"]
    finally:
        conn.close()
    assert n >= 1


def test_ambiguous_item_clarified_then_resolved(client):
    client, _db = client
    # Customer 6 never buys oil: no usual-basket tie-break, so "tel" must ask.
    llm.set_mock_json(
        {"lines": [_line("do litre tel", "tel", 2, "litre")], "notes": ""})
    _post(client, 6, "do litre tel")
    assert _wait_for(lambda: _latest_status(client, 6) == "CLARIFYING")
    assert any("1)" in t for t in _out_texts(client, 6))  # numbered options
    _post(client, 6, "1")  # pick Mustard Oil
    assert _wait_for(lambda: _latest_status(client, 6) == "PACKING")
    assert any("Total:" in t for t in _out_texts(client, 6))


def test_short_stock_message(client):
    client, _db = client
    llm.set_mock_json(
        {"lines": [_line("600 kilo cheeni", "cheeni", 600, "kg")], "notes": ""})
    _post(client, 1, "600 kilo cheeni")
    assert _wait_for(lambda: _latest_status(client, 1) == "REJECTED")
    assert any("sirf 500" in t for t in _out_texts(client, 1))


def test_llm_failure_sends_could_not_understand(client, monkeypatch):
    client, db = client

    def _boom(prompt, schema, **k):
        raise llm.LLMUnavailable("ollama down")
    monkeypatch.setattr("app.ai.llm.generate_json", _boom)
    _post(client, 1, "kuch to bolo")
    assert _wait_for(
        lambda: any("Samajh nahi aaya" in t for t in _out_texts(client, 1)))
    conn = get_conn(db)  # order left in NEW, no crash, event logged
    try:
        st = conn.execute(
            "SELECT status FROM orders WHERE customer_id = 1 AND UPPER(status)"
            " NOT IN ('DELIVERED','CANCELLED','REJECTED') ORDER BY id DESC LIMIT 1"
        ).fetchone()["status"]
    finally:
        conn.close()
    assert st == "NEW"


def test_health_and_customer_page(client):
    client, _db = client
    h = client.get("/api/health").json()
    assert set(h) >= {"ollama", "models", "agent"}
    page = client.get("/customer/1")
    assert page.status_code == 200 and "Munshi" in page.text
    assert client.get("/customer/999").status_code == 404
    bad = client.post("/api/customer/1/message", data={})
    assert bad.status_code == 400
