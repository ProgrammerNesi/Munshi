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


def test_mock_mode_still_processes_a_typed_order_and_shows_agent_trace(
        client, monkeypatch):
    client, db = client
    llm.set_mock_json({"lines": [], "notes": ""})
    state_client = client
    monkeypatch.setenv("MUNSHI_AGENT", "1")
    _post(state_client, 1, "2 kg sugar and 1 kg atta")

    assert _wait_for(lambda: _latest_status(state_client, 1) == "PACKING")
    state = _state(state_client, 1)
    order = next(o for o in state["orders"] if o["status"] == "PACKING")
    assert [(line["name"], line["qty"]) for line in order["lines"]] == [
        ("Sugar", 2), ("Wheat Flour (Atta)", 1),
    ]
    assert not any("[DEMO HISTORY]" in message["text"]
                   for message in state["messages"])
    outbound = [message for message in state["messages"]
                if message["direction"] == "out"
                and message["order_id"] == order["id"]]
    assert any("Your order summary:" in message["text"]
               and "/t/" in message["text"] for message in outbound)
    conn = get_conn(db)
    try:
        kinds = {row["kind"] for row in conn.execute(
            "SELECT kind FROM events WHERE order_id = ?", (order["id"],))}
    finally:
        conn.close()
    assert "agent_fallback" in kinds


def test_credit_over_limit_waits_for_owner(client):
    client, db = client
    llm.set_mock_json({"lines": [_line("300 basmati", "basmati", 300, "kg"),
                                  _line("100 cheeni", "cheeni", 100, "kg")],
                       "notes": ""})
    _post(client, 4, "bada order")
    assert _wait_for(lambda: _latest_status(client, 4) == "AWAITING_APPROVAL")
    assert any("shop approval" in t.lower() for t in _out_texts(client, 4))
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


def test_missing_quantity_then_customer_reply_completes_order(client, monkeypatch):
    client, db = client
    monkeypatch.setenv("MUNSHI_AGENT", "1")

    _post(client, 1, "2 kg sugar and atta")
    assert _wait_for(lambda: _latest_status(client, 1) == "CLARIFYING")
    first = _state(client, 1)
    assert any("How much Wheat Flour (Atta)" in message["text"]
               for message in first["messages"]
               if message["direction"] == "out")

    _post(client, 1, "1 kg")
    assert _wait_for(lambda: _latest_status(client, 1) == "PACKING")
    state = _state(client, 1)
    order = state["orders"][0]
    assert [(line["name"], line["qty"]) for line in order["lines"]] == [
        ("Sugar", 2), ("Wheat Flour (Atta)", 1),
    ]
    outbound = [message["text"] for message in state["messages"]
                if message["direction"] == "out"
                and message["order_id"] == order["id"]]
    assert any("Your order is being packed." in text for text in outbound)
    assert any("Your order summary:" in text and "/t/" in text
               for text in outbound)
    assert not any("couldn't read that order" in text.lower()
                   for text in outbound)

    conn = get_conn(db)
    try:
        events = {row["kind"] for row in conn.execute(
            "SELECT kind FROM events WHERE order_id = ?", (order["id"],))}
    finally:
        conn.close()
    assert {"agent_fallback", "decision", "act"} <= events

    conn = get_conn(db)
    try:
        counts = {
            str(row["item_id"]): row["qty"]
            for row in conn.execute(
                "SELECT item_id, qty FROM order_lines WHERE order_id = ?",
                (order["id"],),
            )
        }
    finally:
        conn.close()
    packed = client.post(
        f"/api/tasks/packing/{order['id']}", json={"counts": counts})
    assert packed.json()["status"] == "READY_FOR_DELIVERY"
    assert client.post(
        f"/api/tasks/delivery/{order['id']}/start").json()["status"] \
        == "OUT_FOR_DELIVERY"
    delivered = client.post(
        f"/api/tasks/delivery/{order['id']}/delivered",
        json={"payment_mode": "cash"},
    )
    assert delivered.json()["status"] == "DELIVERED"
    assert client.get(
        f"/api/track/{order['tracking_url'].rsplit('/', 1)[-1]}"
    ).json()["status"] == "DELIVERED"


def test_short_stock_message(client):
    client, _db = client
    llm.set_mock_json(
        {"lines": [_line("600 kilo cheeni", "cheeni", 600, "kg")], "notes": ""})
    _post(client, 1, "600 kilo cheeni")
    assert _wait_for(lambda: _latest_status(client, 1) == "REJECTED")
    assert any("only 500" in t.lower() for t in _out_texts(client, 1))


def test_llm_failure_falls_back_to_clarifying_unknown_text(client, monkeypatch):
    client, db = client
    monkeypatch.setenv("MUNSHI_MOCK_AI", "0")
    monkeypatch.setenv("MUNSHI_AGENT", "0")

    def _boom(prompt, schema, **k):
        raise llm.LLMUnavailable("ollama down")
    monkeypatch.setattr("app.ai.llm.generate_json", _boom)
    _post(client, 1, "kuch to bolo")
    assert _wait_for(lambda: any("which item did you mean" in t.lower()
                                 for t in _out_texts(client, 1)))
    assert _state(client, 1)["orders"][0]["status"] == "CLARIFYING"
    conn = get_conn(db)  # clarification remains active after the fallback.
    try:
        st = conn.execute(
            "SELECT status FROM orders WHERE customer_id = 1 AND UPPER(status)"
            " NOT IN ('DELIVERED','CANCELLED','REJECTED') ORDER BY id DESC LIMIT 1"
        ).fetchone()["status"]
    finally:
        conn.close()
    assert st == "CLARIFYING"


def test_health_and_customer_page(client):
    client, _db = client
    h = client.get("/api/health").json()
    assert set(h) >= {"ollama", "models", "agent"}
    page = client.get("/customer/1")
    assert page.status_code == 200 and "Munshi" in page.text
    assert client.get("/customer/999").status_code == 404
    bad = client.post("/api/customer/1/message", data={})
    assert bad.status_code == 400


def test_empty_audio_is_rejected_without_creating_a_chat_message(client):
    client, _db = client
    before = len(_state(client, 1)["messages"])
    response = client.post(
        "/api/customer/1/message",
        files={"audio": ("mic.webm", b"", "audio/webm")},
    )
    assert response.status_code == 400
    assert "audio recording is empty" in response.json()["detail"].lower()
    assert len(_state(client, 1)["messages"]) == before
