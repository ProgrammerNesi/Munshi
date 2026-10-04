"""Safe customer tracking links and their public JSON payload."""

from __future__ import annotations

import json
import os

from app.db import get_conn

STEP_LABELS = (
    ("received", "Received"),
    ("confirmed", "Confirmed"),
    ("packing", "Packing"),
    ("ready", "Ready"),
    ("out", "Out for delivery"),
    ("delivered", "Delivered"),
)
EVENT_STEPS = {
    "approve": "confirmed",
    "confirm": "confirmed",
    "assign": "packing",
    "packed": "ready",
    "accept_partial": "ready",
    "dispatch": "out",
    "delivered": "delivered",
}
STATUS_STEPS = {
    "NEW": "received",
    "CLARIFYING": "received",
    "NEEDS_OWNER_APPROVAL": "received",
    "AWAITING_APPROVAL": "received",
    "NEEDS_CUSTOMER_CONFIRM": "received",
    "CONFIRMED": "confirmed",
    "PACKING": "packing",
    "PACK_MISMATCH": "packing",
    "READY_FOR_DELIVERY": "ready",
    "OUT_FOR_DELIVERY": "out",
    "DELIVERED": "delivered",
}
AWAITING_APPROVAL = {"NEEDS_OWNER_APPROVAL", "AWAITING_APPROVAL"}
AWAITING_LABELS = {
    "NEW": "Order received",
    "CLARIFYING": "Please reply to the question in chat",
    "NEEDS_OWNER_APPROVAL": "Awaiting shop approval",
    "AWAITING_APPROVAL": "Awaiting shop approval",
    "NEEDS_CUSTOMER_CONFIRM": "Waiting for your confirmation",
    "PACK_MISMATCH": "Packing is being checked",
    "CANCELLED": "Order cancelled",
    "REJECTED": "Order could not be completed",
}
SHOP_NAME = "Munshi Wholesale"


def base_url() -> str:
    """Customer-facing base URL for links (env override for demos)."""
    return os.environ.get("MUNSHI_BASE_URL", "http://localhost:8000").rstrip("/")


def link(token: str) -> str:
    """Full tracking URL for one unguessable token."""
    return f"{base_url()}/t/{token}"


def _event_step(event: dict) -> str | None:
    """Return the reached step represented by one event, if any."""
    if event["kind"] == "decision":
        try:
            action = json.loads(event["data_json"] or "{}").get("action")
        except (TypeError, ValueError):
            return None
        return "confirmed" if action == "AUTO_CONFIRM" else None
    return EVENT_STEPS.get(event["kind"])


def steps_for(order_id: int, db_path=None, status: str | None = None) -> list[dict]:
    """Return safe step labels, timestamps, and the highlighted current step."""
    conn = get_conn(db_path)
    try:
        order = conn.execute(
            "SELECT status, created_at FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT kind, ts, data_json FROM events WHERE order_id = ? ORDER BY id",
            (order_id,),
        ).fetchall()
    finally:
        conn.close()

    created = next((event["ts"] for event in events
                    if event["kind"] == "created"), order["created_at"])
    reached = {"received": created}
    for event in events:
        step = _event_step(event)
        if step and step not in reached:
            reached[step] = event["ts"]

    live_status = (status or order["status"]).upper()
    current = STATUS_STEPS.get(live_status)
    if current not in reached:
        current = next(
            (step[0] for step in reversed(STEP_LABELS) if step[0] in reached),
            "received",
        )
    return [
        {"key": key, "label": label, "at": reached.get(key),
         "done": key in reached, "current": key == current}
        for key, label in STEP_LABELS
    ]


def track_json(token: str, db_path=None) -> dict | None:
    """Public tracking data only; never return identifiers or private records."""
    from app import bill as bill_mod

    conn = get_conn(db_path)
    try:
        order = conn.execute(
            "SELECT id, status, eta_at FROM orders WHERE track_token = ?",
            (token,),
        ).fetchone()
        if order is None:
            return None
        order_id = order["id"]
        status = order["status"].upper()
        delayed = conn.execute(
            "SELECT 1 FROM attention_items WHERE order_id = ?"
            " AND status = 'open' AND level >= 2 LIMIT 1",
            (order_id,),
        ).fetchone() is not None
        bill = bill_mod.build_bill(order_id, db_path)
        payload = {
            "shop_name": SHOP_NAME,
            "status": status,
            "eta_at": order["eta_at"],
            "delayed": delayed,
            "bill": {
                "lines": [
                    {"name": line["name"], "qty": line["qty"],
                     "unit": line["unit"]}
                    for line in bill["lines"]
                ],
                "total": bill["total"],
                "payment_mode": bill["payment_mode"],
            },
        }
        if status != "DELIVERED":
            payload["steps"] = steps_for(order_id, db_path, status)
            if status in AWAITING_LABELS:
                payload["awaiting"] = AWAITING_LABELS[status]
        else:
            payload["steps"] = []
        if status in AWAITING_APPROVAL:
            payload["awaiting"] = "Awaiting shop approval"
        return payload
    finally:
        conn.close()
