"""Public tracking API and the Scene D delay-to-delivery walkthrough."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import clock  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.main import app  # noqa: E402
from scripts.demo_setup import DEMO_CUSTOMER_ID, scene_b, scene_d  # noqa: E402
from scripts.seed import seed  # noqa: E402


@pytest.fixture
def demo_db(tmp_path, monkeypatch):
    path = tmp_path / "tracking.db"
    seed(path)
    monkeypatch.setenv("MUNSHI_DB", str(path))
    monkeypatch.setenv("MUNSHI_DEMO", "1")
    monkeypatch.setenv("MUNSHI_BASE_URL", "http://localhost:8000")
    clock.reset()
    yield str(path)
    clock.reset()


def _token(db, order_id):
    conn = get_conn(db)
    try:
        return conn.execute(
            "SELECT track_token FROM orders WHERE id = ?", (order_id,)
        ).fetchone()["track_token"]
    finally:
        conn.close()


def test_tracking_json_is_safe_and_has_event_times(demo_db):
    client = TestClient(app)
    oid = scene_d(demo_db)
    response = client.get(f"/api/track/{_token(demo_db, oid)}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["shop_name"] == "Munshi Wholesale"
    assert [step["label"] for step in payload["steps"]] == [
        "Received", "Confirmed", "Packing", "Ready", "Out for delivery",
        "Delivered",
    ]
    assert payload["steps"][0]["at"]
    conn = get_conn(demo_db)
    try:
        confirmed_at = conn.execute(
            "SELECT ts FROM events WHERE order_id = ? AND kind = 'decision'",
            (oid,),
        ).fetchone()["ts"]
        packing_at = conn.execute(
            "SELECT ts FROM events WHERE order_id = ? AND kind = 'assign'",
            (oid,),
        ).fetchone()["ts"]
    finally:
        conn.close()
    assert payload["steps"][1]["at"] == confirmed_at
    assert payload["steps"][2]["at"] == packing_at
    assert payload["steps"][2]["current"] is True
    assert payload["bill"]["lines"][0]["qty"] == 2
    assert payload["bill"]["total"] > 0
    assert "payment_mode" in payload["bill"]

    forbidden = {
        "id", "order_id", "customer", "customer_id", "phone", "staff",
        "staff_phone", "packer_id", "delivery_id", "balance",
        "outstanding", "credit_limit", "transcript",
    }

    def check_keys(value):
        if isinstance(value, dict):
            assert forbidden.isdisjoint(value)
            for child in value.values():
                check_keys(child)
        elif isinstance(value, list):
            for child in value:
                check_keys(child)

    check_keys(payload)
    serialized = response.text
    assert "9800000001" not in serialized
    assert "outstanding" not in serialized.lower()
    assert "credit_limit" not in serialized.lower()


def test_awaiting_approval_is_visible_and_base_url_is_configurable(
        demo_db, monkeypatch):
    monkeypatch.setenv("MUNSHI_BASE_URL", "https://shop.example.test")
    client = TestClient(app)
    oid = scene_b(demo_db)
    token = _token(demo_db, oid)
    payload = client.get(f"/api/track/{token}").json()
    assert payload["awaiting"] == "Awaiting shop approval"
    assert payload["steps"][0]["current"] is True
    customer_messages = client.get(
        f"/api/state?customer_id={DEMO_CUSTOMER_ID}"
    ).json()["messages"]
    assert any("https://shop.example.test/t/" in m["text"]
               for m in customer_messages)
    assert client.get(f"/api/owner/order/{oid}").json()["tracking_url"] \
        == f"https://shop.example.test/t/{token}"


def test_unknown_tracking_token_has_friendly_404(demo_db):
    assert Path(demo_db).exists()
    client = TestClient(app)
    page = client.get("/t/not-a-valid-token")
    assert page.status_code == 404
    assert "Tracking link not found" in page.text
    assert client.get("/api/track/not-a-valid-token").status_code == 404


def test_l2_shows_delay_and_owner_reassign_completes_scene_d(demo_db):
    client = TestClient(app)
    oid = scene_d(demo_db)
    token = _token(demo_db, oid)
    link = f"http://localhost:8000/t/{token}"

    assert client.post("/api/demo/skip?minutes=2").status_code == 200
    first = client.post("/api/supervisor/tick").json()
    assert first["l1"] == [oid]
    assert first["l2"] == []

    assert client.post("/api/demo/skip?minutes=1").status_code == 200
    second = client.post("/api/supervisor/tick").json()
    assert second["l2"] == [oid]
    tracked = client.get(f"/api/track/{token}").json()
    assert tracked["delayed"] is True
    page = client.get(f"/t/{token}")
    assert "Der ho rahi hai" in page.text
    assert "There is a delay" in page.text
    assert '<meta name="robots" content="noindex">' in page.text
    assert "<link " not in page.text and '<script src=' not in page.text

    messages = client.get(
        f"/api/state?customer_id={DEMO_CUSTOMER_ID}"
    ).json()["messages"]
    assert any(link in message["text"] for message in messages)
    detail = client.get(f"/api/owner/order/{oid}").json()
    assert detail["tracking_url"] == link
    assert detail["needs_reassign"] is True
    assert client.post(f"/api/owner/order/{oid}/reassign").json()["status"] \
        == "PACKING"

    conn = get_conn(demo_db)
    try:
        line = conn.execute(
            "SELECT item_id, qty FROM order_lines WHERE order_id = ?", (oid,)
        ).fetchone()
    finally:
        conn.close()
    packed = client.post(
        f"/api/tasks/packing/{oid}",
        json={"counts": {str(line["item_id"]): line["qty"]}},
    )
    assert packed.json()["status"] == "READY_FOR_DELIVERY"
    assert client.post(f"/api/tasks/delivery/{oid}/start").json()["status"] \
        == "OUT_FOR_DELIVERY"
    assert client.post(
        f"/api/tasks/delivery/{oid}/delivered", json={"payment_mode": "cash"}
    ).json()["status"] == "DELIVERED"
    finished = client.get(f"/api/track/{token}").json()
    assert finished["status"] == "DELIVERED"
    assert finished["steps"] == []
    assert finished["bill"]["total"] == tracked["bill"]["total"]
