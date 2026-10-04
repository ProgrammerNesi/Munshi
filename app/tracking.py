"""Customer tracking: unguessable links, stepper from events, safe JSON.

Everything here is safe to show the customer: order ids, staff phones,
other customers, balances and credit limits never leave this module.
"""

from __future__ import annotations

import json
import os

from app.db import get_conn

# Vertical stepper: (key, label, event kinds that mark it reached).
STEPS = [
    ("received", "Received", set()),
    ("confirmed", "Confirmed", {"approve", "confirm", "auto_confirm"}),
    ("packing", "Packing", {"assign"}),
    ("ready", "Ready", {"packed", "accept_partial"}),
    ("out", "Out for delivery", {"dispatch"}),
    ("delivered", "Delivered", {"delivered"}),
]

# Statuses shown as "waiting on the shop" instead of the stepper.
AWAITING = {"NEW", "CLARIFYING", "NEEDS_OWNER_APPROVAL", "NEEDS_CUSTOMER_CONFIRM",
            "AWAITING_APPROVAL", "CANCELLED", "REJECTED", "PACK_MISMATCH"}
AWAITING_LABEL = {"NEW": "Order mil gaya hai",
                  "CLARIFYING": "Ek sawal hai — chat me jawab dijiye",
                  "NEEDS_OWNER_APPROVAL": "Dukaan se approval ka intezaar hai",
                  "NEEDS_CUSTOMER_CONFIRM": "Aapke confirm ka intezaar hai",
                  "AWAITING_APPROVAL": "Dukaan se approval ka intezaar hai",
                  "CANCELLED": "Order cancel ho gaya hai",
                  "REJECTED": "Order nahi ho paya",
                  "PACK_MISMATCH": "Packing check ho rahi hai, thoda intezaar"}

DELAY_HINGLISH = "Der ho rahi hai — naya time upar ETA me hai."
DELAY_ENGLISH = "Running late — see the revised ETA above."


def base_url() -> str:
    """Public base URL for tracking links (env override for demos)."""
    return os.environ.get("MUNSHI_BASE_URL", "http://localhost:8000").rstrip("/")


def link(token: str) -> str:
    """Full tracking URL for one unguessable token."""
    return f"{base_url()}/t/{token}"


def _action_of(event: dict) -> str:
    """Rulebook action tucked into decision events ('' when absent)."""
    try:
        return json.loads(event.get("data_json") or "{}").get("action", "")
    except ValueError:
        return ""


def steps_for(order_id: int, db_path=None) -> list[dict]:
    """Stepper timeline: [{key, label, at|null, done, current}]."""
    conn = get_conn(db_path)
    try:
        order = conn.execute(
            "SELECT status, created_at FROM orders WHERE id = ?",
            (order_id,)).fetchone()
        events = conn.execute(
            "SELECT kind, ts, data_json FROM events WHERE order_id = ?"
            " ORDER BY id", (order_id,)).fetchall()
    finally:
        conn.close()
    reached: dict[str, str] = {"received": order["created_at"]}
    for e in events:
        for key, _label, kinds in STEPS:
            if key in reached or e["kind"] not in kinds:
                continue
            if e["kind"] == "decision" and \
                    _action_of({"data_json": e["data_json"]}) != "AUTO_CONFIRM":
                continue
            reached[key] = e["ts"]
    order_keys = [k for k, _l, _k in STEPS if k in reached]
    current = order_keys[-1] if order_keys else "received"
    return [{"key": k, "label": label,
             "at": reached.get(k), "done": k in reached,
             "current": k == current} for k, label, _k in STEPS]


def track_json(token: str, db_path=None) -> dict | None:
    """Safe tracking payload (None for unknown tokens). No sensitive fields."""
    from app import bill as bill_mod  # local: keep imports one-directional

    conn = get_conn(db_path)
    try:
        order = conn.execute(
            "SELECT id, status, eta_at FROM orders WHERE track_token = ?",
            (token,)).fetchone()
        if order is None:
            return None
        oid = order["id"]
        delayed = conn.execute(
            "SELECT COUNT(*) FROM attention_items WHERE order_id = ?"
            " AND status = 'open' AND level >= 2", (oid,)).fetchone()[0] > 0
        bill = bill_mod.build_bill(oid, db_path)
        payload = {
            "status": order["status"],
            "eta_at": order["eta_at"],
            "delayed": bool(delayed),
            "bill": {"lines": [{"name": ln["name"], "qty": ln["qty"],
                                "unit": ln["unit"]} for ln in bill["lines"]],
                     "total": bill["total"],
                     "payment_mode": bill["payment_mode"]},
        }
        if order["status"] == "DELIVERED":
            payload["steps"] = []
        elif order["status"] in AWAITING:
            payload["awaiting"] = AWAITING_LABEL[order["status"]]
            payload["steps"] = []
        else:
            payload["steps"] = steps_for(oid, db_path)
        return payload
    finally:
        conn.close()
