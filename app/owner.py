"""Owner portal queries: board, detail+timeline, khata, stock, rules, inbox.

All read-only except save_rules / mark_done (explicit owner writes).
Boring SQL over the Phase 1 schema; no LLM anywhere here.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import yaml

from app import clock
from app.db import get_conn

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "rules.yaml"  # monkeypatchable in tests; never touch prod file there

NEEDS_YOU = ("AWAITING_APPROVAL", "PACK_MISMATCH")
TERMINAL = ("DELIVERED", "CANCELLED", "REJECTED")
AGENT_KINDS = ("agent_tool", "agent_proposal", "agent_overridden", "agent_fallback")

# Expected shape of rules.yaml: dotted key -> python type. POST validation.
RULE_TYPES = {
    "credit.require_owner_approval_above_outstanding_plus_order": bool,
    "order.auto_confirm_max_amount": (int, float),
    "order.unusual_qty_multiplier": (int, float),
    "customers.new_customer_requires_approval": bool,
    "stock.allow_partial_when_short": bool,
    "packing.mismatch_tolerance": (int, float),
    "delivery.fee": (int, float),
    "delivery.free_above": (int, float),
    "watchdog.packing_max_minutes": (int, float),
    "watchdog.delivery_max_minutes": (int, float),
    "supervision.packing_late": (int, float),
    "supervision.ready_wait": (int, float),
    "supervision.delivery_late": (int, float),
    "supervision.escalation_gap": (int, float),
}


def _now() -> str:
    """Demo-aware timestamp for ts / updated_at columns."""
    return clock.now().isoformat()


def _seconds(data_json: str) -> float | None:
    """Per-step seconds tucked into every event's data_json (may be absent)."""
    try:
        return json.loads(data_json or "{}").get("seconds")
    except (ValueError, AttributeError):
        return None


def _events_for(conn, order_id: int) -> list[dict]:
    """Timeline rows for one order, oldest first, seconds parsed out."""
    return [
        {"id": r["id"], "actor": r["actor"], "kind": r["kind"],
         "message": r["message"], "seconds": _seconds(r["data_json"]),
         "ts": r["ts"]}
        for r in conn.execute(
            "SELECT * FROM events WHERE order_id = ? ORDER BY id", (order_id,))
    ]


def _latest_message(conn, order_id: int, kind: str) -> str:
    """Newest event message of one kind ("" when the step never ran)."""
    row = conn.execute(
        "SELECT message FROM events WHERE order_id = ? AND kind = ?"
        " ORDER BY id DESC LIMIT 1", (order_id, kind)).fetchone()
    return row["message"] if row else ""


def _rulebook_reasons(conn, order_id: int) -> list[str]:
    """Rulebook sentences off the latest decision event (human-readable)."""
    msg = _latest_message(conn, order_id, "decision")
    if ": " not in msg:
        return [msg] if msg else []
    return [s.strip() for s in msg.split(": ", 1)[1].split("; ") if s.strip()]


def board(db_path=None) -> dict:
    """Shop orders grouped by status, excluding synthetic seed history."""
    conn = get_conn(db_path)
    try:
        groups: dict[str, list] = {}
        needs: list = []
        rows = conn.execute(
            "SELECT * FROM orders"
            " WHERE COALESCE(transcript, '') NOT LIKE '[DEMO HISTORY]%'"
            " ORDER BY id DESC LIMIT 100"
        ).fetchall()
        for o in rows:
            cust = conn.execute(
                "SELECT name FROM customers WHERE id = ?",
                (o["customer_id"],)).fetchone()
            card = {"id": o["id"], "status": o["status"], "total": o["total"],
                    "customer": cust["name"] if cust else "?",
                    "updated_at": o["updated_at"]}
            groups.setdefault(o["status"], []).append(card)
            if o["status"] in NEEDS_YOU:
                prop = _latest_message(conn, o["id"], "agent_proposal")
                card = {**card, "reasons": _rulebook_reasons(conn, o["id"]),
                        "agent_note": prop}
                if o["status"] == "PACK_MISMATCH":
                    card["reasons"] = [_latest_message(conn, o["id"], "mismatch")]
                needs.append(card)
        active = sum(
            len(orders) for status, orders in groups.items()
            if status not in TERMINAL
        )
        return {
            "groups": groups,
            "needs_you": needs,
            "summary": {
                "orders": len(rows),
                "active": active,
                "needs_you": len(needs),
                "delivered": len(groups.get("DELIVERED", [])),
            },
        }
    finally:
        conn.close()


def order_detail(order_id: int, db_path=None) -> dict:
    """Transcript, parsed lines, bill totals, full reasoning timeline."""
    from app import bill as bill_mod  # local: owner.py stays import-light
    from app import tracking

    conn = get_conn(db_path)
    try:
        o = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if o is None:
            raise KeyError(f"No order #{order_id}.")
        cust = conn.execute(
            "SELECT name FROM customers WHERE id = ?",
            (o["customer_id"],)).fetchone()
        lines = [dict(r) for r in conn.execute(
            "SELECT ol.qty, ol.unit_price, ol.packed_qty, i.name, i.unit"
            " FROM order_lines ol JOIN items i ON i.id = ol.item_id"
            " WHERE ol.order_id = ? ORDER BY ol.id", (order_id,))]
        try:
            bill = bill_mod.build_bill(order_id, db_path)
        except Exception:
            bill = None
        needs_reassign = conn.execute(
            "SELECT 1 FROM attention_items WHERE order_id = ?"
            " AND status = 'open' AND level >= 2 LIMIT 1",
            (order_id,),
        ).fetchone() is not None
        events = _events_for(conn, order_id)
        return {"id": o["id"], "status": o["status"],
                "customer": cust["name"] if cust else "?",
                "transcript": o["transcript"], "total": o["total"],
                "payment_mode": o["payment_mode"], "updated_at": o["updated_at"],
                "lines": lines, "bill": bill, "timeline": events,
                "tracking_url": tracking.link(o["track_token"]),
                "needs_reassign": needs_reassign and o["status"] == "PACKING",
                "has_agent": any(e["kind"] in AGENT_KINDS for e in events)}
    finally:
        conn.close()


def khata(db_path=None) -> list[dict]:
    """One row per customer: outstanding vs limit bar + last order date."""
    conn = get_conn(db_path)
    try:
        rows = []
        for c in conn.execute("SELECT * FROM customers ORDER BY outstanding DESC"):
            last = conn.execute(
                "SELECT MAX(created_at) AS at FROM orders WHERE customer_id = ?",
                (c["id"],)).fetchone()["at"]
            limit = c["credit_limit"] or 0
            rows.append({"id": c["id"], "name": c["name"],
                         "outstanding": c["outstanding"], "limit": limit,
                         "pct": round(100 * c["outstanding"] / limit)
                         if limit > 0 else (100 if c["outstanding"] > 0 else 0),
                         "last_order": (last or "")[:10]})
        return rows
    finally:
        conn.close()


def stock_report(db_path=None, days: int = 14) -> list[dict]:
    """Items with days-of-stock-left from the last `days` of delivered usage."""
    cutoff = (clock.now() - timedelta(days=days)).isoformat()
    conn = get_conn(db_path)
    try:
        used: dict[int, float] = {}
        for r in conn.execute(
                "SELECT ol.item_id, SUM(ol.qty) AS q FROM order_lines ol"
                " JOIN orders o ON o.id = ol.order_id"
                " WHERE UPPER(o.status) = 'DELIVERED' AND o.created_at >= ?"
                " GROUP BY ol.item_id", (cutoff,)):
            used[r["item_id"]] = r["q"] or 0
        rows = []
        for it in conn.execute("SELECT * FROM items ORDER BY name"):
            per_day = used.get(it["id"], 0) / days
            left = round(it["stock_qty"] / per_day, 1) if per_day > 0 else None
            rows.append({"id": it["id"], "name": it["name"], "unit": it["unit"],
                         "stock": it["stock_qty"],
                         "reorder_level": it["reorder_level"],
                         "used_per_day": round(per_day, 2),
                         "days_left": left,
                         "low": it["stock_qty"] <= it["reorder_level"]
                         or (left is not None and left < 7)})
        return rows
    finally:
        conn.close()


def rules_text() -> str:
    """Raw rules.yaml (comments included) for the editable textarea."""
    return RULES_PATH.read_text(encoding="utf-8")


def save_rules(text: str, db_path=None) -> dict:
    """Validate edited YAML (shape + types), save, log an event. Never half-write."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML does not parse: {e}")
    if not isinstance(doc, dict):
        raise ValueError("Top level must be a mapping.")
    problems = []
    for dotted, want in RULE_TYPES.items():
        section, key = dotted.split(".", 1)
        got = (doc.get(section) or {}).get(key, None)
        # bool is a subclass of int: demand exact bool where a bool is wanted.
        ok = isinstance(got, bool) if want is bool \
            else isinstance(got, want) and not isinstance(got, bool)
        if got is None:
            problems.append(f"missing: {dotted}")
        elif not ok:
            problems.append(f"{dotted} must be {want}, got {got!r}")
        elif dotted.startswith("supervision.") and got <= 0:
            problems.append(f"{dotted} must be above 0 minutes.")
    if problems:
        raise ValueError("; ".join(problems))
    RULES_PATH.write_text(text, encoding="utf-8")
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO events (order_id, actor, kind, message, data_json, ts)"
            " VALUES (NULL, 'owner', 'rules_updated',"
            " 'Owner updated the rulebook; it applies immediately.', '{}', ?)",
            (_now(),))
        conn.commit()
    finally:
        conn.close()
    return {"saved": True}


def notifications(db_path=None, limit: int = 50) -> dict:
    """Owner/packer inbox, newest first, plus the bell counter."""
    conn = get_conn(db_path)
    try:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))]
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE done = 0"
        ).fetchone()["n"]
        return {"items": items, "unread": unread}
    finally:
        conn.close()


def mark_done(notification_id: int, db_path=None) -> None:
    """Clear one notification (KeyError when unknown)."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE notifications SET done = 1 WHERE id = ?",
                           (notification_id,))
        if cur.rowcount == 0:
            raise KeyError(f"No notification #{notification_id}.")
        conn.commit()
    finally:
        conn.close()


def last_order_stages(db_path=None) -> dict:
    """Seconds per step for the newest order (health strip + debugging)."""
    conn = get_conn(db_path)
    try:
        last = conn.execute(
            "SELECT id FROM orders ORDER BY id DESC LIMIT 1").fetchone()
        if last is None:
            return {"order_id": None, "stages": {}}
        stages: dict[str, float] = {}
        for r in conn.execute(
                "SELECT kind, data_json FROM events WHERE order_id = ?",
                (last["id"],)):
            s = _seconds(r["data_json"])
            if s is not None:
                stages[r["kind"]] = round(stages.get(r["kind"], 0) + s, 2)
        return {"order_id": last["id"], "stages": stages}
    finally:
        conn.close()
