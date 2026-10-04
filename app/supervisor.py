"""Deterministic delay supervisor. No LLM: SLAs in, engine calls out.

scan_once() finds orders stuck in PACKING / READY_FOR_DELIVERY /
OUT_FOR_DELIVERY past their rules.yaml SLA and escalates:
  L1: notification to the assigned packer/delivery person.
  L2 (sla + escalation_gap): reassign to the other least-busy staffer,
      notify the owner with the reason, message the customer once with a
      tracking link and a new ETA.
One open attention_items row per (order, kind); rows resolve when the order
leaves the stage. Every action logs an events row with actor="supervisor".
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta

from app import clock
from app import engine
from app import messages as _messages
from app.db import get_conn
from app.rules import load_rules, supervision_minutes

# Order status -> (attention kind, sla key, staffer role, id column).
STAGES = {
    "PACKING": ("packing_late", "packing_late", "packer", "packer_id"),
    "READY_FOR_DELIVERY": ("ready_not_picked", "ready_wait", "delivery",
                            "delivery_id"),
    "OUT_FOR_DELIVERY": ("delivery_late", "delivery_late", "delivery",
                          "delivery_id"),
}

REASONS = {
    "packing_late": "packing atki hui hai",
    "ready_not_picked": "packed order pickup ka wait kar raha hai",
    "delivery_late": "delivery bahar atki hui hai",
}


def _demo() -> bool:
    """Demo mode compresses every SLA 10x (minimum 1 minute)."""
    return os.environ.get("MUNSHI_DEMO", "0") == "1"


def _log(conn, order_id, kind, message, data=None) -> None:
    """Supervisor reasoning-log row (seconds always 0.0: no model ran)."""
    conn.execute(
        "INSERT INTO events (order_id, actor, kind, message, data_json, ts)"
        " VALUES (?,?,?,?,?,?)",
        (order_id, "supervisor", kind, message,
         json.dumps({**(data or {}), "seconds": 0.0}), clock.now().isoformat()),
    )


def _late_minutes(entered_at: str | None, now: datetime) -> int | None:
    """Whole minutes in the current stage (None when unknown)."""
    if not entered_at:
        return None
    return int((now - datetime.fromisoformat(entered_at)).total_seconds() // 60)


def _open_item(conn, order_id: int, kind: str):
    """The open attention row for one (order, kind), if any."""
    return conn.execute(
        "SELECT * FROM attention_items WHERE order_id = ? AND kind = ?"
        " AND status = 'open'",
        (order_id, kind)).fetchone()


def _assignee(conn, order: dict, role: str, id_col: str):
    """Assigned staffer row (id, name); None when nobody assigned yet."""
    if order[id_col] is None:
        return None
    return conn.execute("SELECT id, name FROM staff WHERE id = ?",
                        (order[id_col],)).fetchone()


def _other_least_busy(conn, role: str, exclude_id: int | None) -> dict | None:
    """Least-busy staffer of a role, preferring someone else. Never None."""
    states = ["PACKING"] if role == "packer" else \
        ["READY_FOR_DELIVERY", "OUT_FOR_DELIVERY"]
    placeholders = ",".join("?" for _ in states)
    col = "packer_id" if role == "packer" else "delivery_id"
    rows = conn.execute(
        f"SELECT s.id, s.name, COUNT(o.id) AS n FROM staff s"
        f" LEFT JOIN orders o ON o.{col} = s.id AND o.status IN ({placeholders})"
        f" WHERE s.role = ? GROUP BY s.id ORDER BY n, s.id",
        (*states, role)).fetchall()
    if not rows:
        raise engine.EngineError(f"No {role} on duty.")
    for r in rows:
        if r["id"] != exclude_id:
            return {"id": r["id"], "name": r["name"]}
    return {"id": rows[0]["id"], "name": rows[0]["name"]}


def _fire_l1(conn, order: dict, kind: str, late: int, staffer, role: str) -> None:
    """Nudge the assigned staffer (or the role, if unassigned)."""
    engine._notify(conn, role, staffer["id"] if staffer else None, order["id"],
                   _messages.delay_nudge_staff(order["id"], late),
                   action_required=True)
    _log(conn, order["id"], "nudge",
         f"L1: {kind} {late} min late, nudged"
         f" {staffer['name'] if staffer else role}.",
         {"kind": kind, "level": 1, "late_min": late})


def _fire_l2(conn, order: dict, kind: str, late: int, sla: int, gap: int,
             staffer, role: str, id_col: str) -> None:
    """Reassign, tell the owner why, message the customer once with ETA."""
    now = clock.now()
    other = _other_least_busy(conn, role,
                              staffer["id"] if staffer else None)
    conn.execute(f"UPDATE orders SET {id_col} = ?, updated_at = ? WHERE id = ?",
                 (other["id"], now.isoformat(), order["id"]))
    engine._notify(conn, role, other["id"], order["id"],
                   f"Order #{order['id']} aapko diya gaya hai,"
                   " turant shuru karein.",
                   action_required=True)
    _log(conn, order["id"], "reassign",
         f"L2: reassigned {role} {staffer['name'] if staffer else '?'}"
         f" -> {other['name']}.",
         {"kind": kind, "level": 2, "from": staffer["id"] if staffer else None,
          "to": other["id"]})
    reason = f"{REASONS[kind]} ({late} min, limit {sla} min)."
    engine._notify(conn, "owner", None, order["id"],
                   _messages.delay_owner_alert(order["id"], reason,
                                               other["name"]),
                   action_required=True)
    _log(conn, order["id"], "escalate", f"L2: owner looped in. {reason}",
         {"kind": kind, "level": 2})
    token = order["track_token"]
    if not token:
        token = secrets.token_urlsafe(16)
        conn.execute("UPDATE orders SET track_token = ? WHERE id = ?",
                     (token, order["id"]))
    eta_min = gap + sla
    eta_at = (now + timedelta(minutes=eta_min)).isoformat()
    conn.execute("UPDATE orders SET eta_at = ? WHERE id = ?",
                 (eta_at, order["id"]))
    engine._tell_customer(
        conn, order["id"],
        _messages.delay_customer(f"lagbhag {eta_min} min me", f"/t/{token}"))
    _log(conn, order["id"], "delay_notice",
         f"L2: customer told, new ETA ~{eta_min} min via /t/<token>.",
         {"kind": kind, "level": 2, "eta_at": eta_at})


def _kind_for_status(status: str) -> str | None:
    """Attention kind watched for a status (None when unwatched)."""
    return STAGES[status][0] if status in STAGES else None


def _resolve_stale(conn, order_id: int, status: str, now: datetime) -> int:
    """Resolve open items whose stage this order already left. Returns count."""
    want = _kind_for_status(status)
    n = 0
    for r in conn.execute(
            "SELECT * FROM attention_items WHERE order_id = ? AND status = 'open'",
            (order_id,)):
        if r["kind"] != want:
            conn.execute(
                "UPDATE attention_items SET status = 'resolved', resolved_at = ?"
                " WHERE id = ?", (now.isoformat(), r["id"]))
            _log(conn, order_id, "resolve",
                 f"Attention {r['kind']} resolved: order moved to {status}.",
                 {"kind": r["kind"]})
            n += 1
    return n


def _handle_order(conn, order: dict, slas: dict, now: datetime) -> dict:
    """One stuck order: fire new levels. Returns counts."""
    counts = {"l1": 0, "l2": 0, "resolved": 0}
    kind, sla_key, role, id_col = STAGES[order["status"]]
    sla, gap = slas[sla_key], slas["escalation_gap"]
    counts["resolved"] = _resolve_stale(conn, order["id"], order["status"], now)
    late = _late_minutes(order["stage_entered_at"], now)
    if late is None or late < sla:
        return counts
    staffer = _assignee(conn, order, role, id_col)
    if staffer:
        staffer = {"id": staffer["id"], "name": staffer["name"]}
    target = 2 if late >= sla + gap else 1
    item = _open_item(conn, order["id"], kind)
    start = item["level"] if item else 0
    if item is None:
        conn.execute(
            "INSERT INTO attention_items (order_id, kind, level, status,"
            " opened_at, resolved_at) VALUES (?,?,?,?,?,NULL)",
            (order["id"], kind, 0, "open", now.isoformat()))
        item = _open_item(conn, order["id"], kind)
    for level in range(start + 1, target + 1):
        if level == 1:
            _fire_l1(conn, order, kind, late, staffer, role)
            counts["l1"] += 1
        else:
            _fire_l2(conn, order, kind, late, sla, gap, staffer, role, id_col)
            counts["l2"] += 1
        conn.execute("UPDATE attention_items SET level = ? WHERE id = ?",
                     (level, item["id"]))
    return counts


def scan_once(db_path=None, rules: dict | None = None) -> dict:
    """One deterministic sweep. Per-order errors never stop the scan."""
    from app.rules import load_rules, supervision_minutes

    rules = rules if rules is not None else load_rules()
    slas = supervision_minutes(rules, demo=_demo())
    conn = get_conn(db_path)
    summary: dict = {"checked": 0, "l1": [], "l2": [], "resolved": [],
                     "errors": []}
    try:
        now = clock.now()
        # Resolve items on orders sitting in unwatched (e.g. terminal) states.
        stray = conn.execute(
            "SELECT DISTINCT o.id, o.status FROM orders o"
            " JOIN attention_items a ON a.order_id = o.id"
            " WHERE a.status = 'open' AND o.status NOT IN"
            " ('PACKING','READY_FOR_DELIVERY','OUT_FOR_DELIVERY')").fetchall()
        for row in stray:
            try:
                n = _resolve_stale(conn, row["id"], row["status"], now)
                conn.commit()
                if n:
                    summary["resolved"].append(row["id"])
            except Exception as e:  # noqa: BLE001 — one bad order, keep going
                conn.rollback()
                summary["errors"].append(
                    {"order_id": row["id"], "error": str(e)[:200]})
        orders = conn.execute(
            "SELECT * FROM orders WHERE status IN"
            " ('PACKING','READY_FOR_DELIVERY','OUT_FOR_DELIVERY')").fetchall()
        for order in orders:
            order = dict(order)
            # Resolve items for orders that already left every watched stage.
            try:
                counts = _handle_order(conn, order, slas, now)
                conn.commit()
            except Exception as e:  # noqa: BLE001 — one bad order, keep going
                conn.rollback()
                summary["errors"].append({"order_id": order["id"], "error": str(e)[:200]})
                continue
            summary["checked"] += 1
            if counts["l1"]:
                summary["l1"].append(order["id"])
            if counts["l2"]:
                summary["l2"].append(order["id"])
            if counts["resolved"]:
                summary["resolved"].append(order["id"])
        return summary
    finally:
        conn.close()


async def _loop(db_path=None) -> None:
    """Sweep every 30s (5s in demo mode). One bad tick never kills the loop."""
    import asyncio

    while True:
        try:
            interval = 5 if _demo() else 30
            await asyncio.sleep(interval)
            scan_once(db_path)
        except Exception:  # noqa: BLE001 — config/DB trouble; retry next tick
            try:
                conn = get_conn(db_path)
                try:
                    conn.execute(
                        "INSERT INTO events (order_id, actor, kind, message,"
                        " data_json, ts) VALUES (NULL, 'supervisor', 'error',"
                        " 'Supervisor tick failed; will retry.', '{}', ?)",
                        (clock.now().isoformat(),))
                    conn.commit()
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001 — truly nothing more to do
                pass


def start_loop(db_path=None) -> None:
    """Fire-and-forget the sweep loop (call once, from app startup)."""
    import asyncio

    asyncio.get_running_loop().create_task(_loop(db_path), name="munshi-super")
