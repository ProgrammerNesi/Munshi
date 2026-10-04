"""Order state machine: explicit transitions, every step logged to `events`.

States (AGENTS.md leaves the exact list to the implementer; this is it):
  NEW -> NEEDS_OWNER_APPROVAL | NEEDS_CUSTOMER_CONFIRM | CONFIRMED
      -> REJECTED | CANCELLED            (apply_decision / cancel)
  NEEDS_OWNER_APPROVAL -> CONFIRMED | CANCELLED      (owner_approve / decline)
  NEEDS_CUSTOMER_CONFIRM -> CONFIRMED | CANCELLED    (customer_confirm / cancel)
  CONFIRMED -> PACKING | CANCELLED                  (assign_staff / cancel)
  PACKING -> PACK_MISMATCH | READY_FOR_DELIVERY     (submit_pack_counts)
  PACK_MISMATCH -> PACKING | READY_FOR_DELIVERY | CANCELLED   (owner_resolve_mismatch)
  READY_FOR_DELIVERY -> OUT_FOR_DELIVERY | CANCELLED (start_delivery / cancel)
  OUT_FOR_DELIVERY -> DELIVERED | READY_FOR_DELIVERY (mark_delivered / problem retry)
  DELIVERED, CANCELLED, REJECTED are terminal.

Money/stock rules: stock is reserved (decremented) at CONFIRMED and released
on cancel; packed-quantity adjustments return the difference to the shelf;
credit sales add the bill to the customer's outstanding (khata) at delivery.
"""

from __future__ import annotations

import secrets
import json
from pathlib import Path

from app import messages as _messages
from app import clock
from app.db import get_conn
from app.rules import (
    ASK_CUSTOMER,
    ASK_OWNER,
    AUTO_CONFIRM,
    REJECT_SHORT_STOCK,
    Decision,
    load_rules,
    rupees,
)

# -- states ---------------------------------------------------------------

NEW = "NEW"
NEEDS_OWNER_APPROVAL = "NEEDS_OWNER_APPROVAL"
NEEDS_CUSTOMER_CONFIRM = "NEEDS_CUSTOMER_CONFIRM"
CONFIRMED = "CONFIRMED"
PACKING = "PACKING"
PACK_MISMATCH = "PACK_MISMATCH"
READY_FOR_DELIVERY = "READY_FOR_DELIVERY"
OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
DELIVERED = "DELIVERED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"

TERMINAL = (DELIVERED, CANCELLED, REJECTED)

# Explicit allow-list: every legal (from, to) hop.
TRANSITIONS: dict[str, set[str]] = {
    NEW: {NEEDS_OWNER_APPROVAL, NEEDS_CUSTOMER_CONFIRM, CONFIRMED, REJECTED, CANCELLED},
    NEEDS_OWNER_APPROVAL: {CONFIRMED, CANCELLED},
    NEEDS_CUSTOMER_CONFIRM: {CONFIRMED, CANCELLED},
    CONFIRMED: {PACKING, CANCELLED},
    PACKING: {PACK_MISMATCH, READY_FOR_DELIVERY},
    PACK_MISMATCH: {PACKING, READY_FOR_DELIVERY, CANCELLED},
    READY_FOR_DELIVERY: {OUT_FOR_DELIVERY, CANCELLED},
    OUT_FOR_DELIVERY: {DELIVERED, READY_FOR_DELIVERY},
    DELIVERED: set(),
    CANCELLED: set(),
    REJECTED: set(),
}

# Decision action -> state entered from NEW.
DECISION_STATE = {
    AUTO_CONFIRM: CONFIRMED,
    ASK_OWNER: NEEDS_OWNER_APPROVAL,
    ASK_CUSTOMER: NEEDS_CUSTOMER_CONFIRM,
    REJECT_SHORT_STOCK: REJECTED,
}


class EngineError(ValueError):
    """Bad order id, bad payload, or unknown staff/owner."""


class InvalidTransition(EngineError):
    """The requested hop is not in TRANSITIONS."""


# -- small helpers --------------------------------------------------------

def _now() -> str:
    """Demo-aware timestamp for ts / updated_at columns."""
    return clock.now().isoformat()


def _rules(rules: dict | None) -> dict:
    """Use the caller's rules or read rules.yaml (cheap, always fresh)."""
    return rules if rules is not None else load_rules()


def _get_order(conn, order_id: int):
    """Fetch the order row or raise EngineError."""
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise EngineError(f"Order #{order_id} not found.")
    return row


def _log(conn, order_id, actor, kind, message, data=None) -> None:
    """Append one human-readable row to the reasoning log (`events`)."""
    conn.execute(
        "INSERT INTO events (order_id, actor, kind, message, data_json, ts)"
        " VALUES (?,?,?,?,?,?)",
        (order_id, actor, kind, message, json.dumps(data or {}), _now()),
    )


def _move(conn, order_id, to_state, actor, kind, message, data=None):
    """Validate the hop, update status, log the event. Returns fresh row."""
    order = _get_order(conn, order_id)
    if to_state not in TRANSITIONS[order["status"]]:
        raise InvalidTransition(
            f"Order #{order_id} cannot go {order['status']} -> {to_state}."
        )
    conn.execute(
        "UPDATE orders SET status = ?, updated_at = ?, stage_entered_at = ?"
        " WHERE id = ?",
        (to_state, _now(), _now(), order_id),
    )
    _log(conn, order_id, actor, kind, message, data)
    return _get_order(conn, order_id)


def _lines(conn, order_id):
    """Order lines with item names, for billing/stock math."""
    return conn.execute(
        "SELECT ol.*, i.name FROM order_lines ol JOIN items i ON i.id = ol.item_id"
        " WHERE ol.order_id = ?",
        (order_id,),
    ).fetchall()


def _reserve(conn, order_id) -> None:
    """Decrement shelf stock by ordered qty (called on entering CONFIRMED)."""
    for ln in _lines(conn, order_id):
        row = conn.execute(
            "SELECT stock_qty FROM items WHERE id = ?", (ln["item_id"],)
        ).fetchone()
        if row["stock_qty"] < ln["qty"]:
            raise EngineError(
                f"Cannot reserve {ln['qty']:g} {ln['name']}:"
                f" only {row['stock_qty']:g} in stock."
            )
        conn.execute(
            "UPDATE items SET stock_qty = stock_qty - ? WHERE id = ?",
            (ln["qty"], ln["item_id"]),
        )


def _release(conn, order_id) -> None:
    """Return the full ordered qty to the shelf (cancel path)."""
    for ln in _lines(conn, order_id):
        conn.execute(
            "UPDATE items SET stock_qty = stock_qty + ? WHERE id = ?",
            (ln["qty"], ln["item_id"]),
        )


def _adjust_to_packed(conn, order_id) -> None:
    """Return (ordered - packed) per line to the shelf (partial path)."""
    for ln in _lines(conn, order_id):
        conn.execute(
            "UPDATE items SET stock_qty = stock_qty + ? WHERE id = ?",
            (ln["qty"] - ln["packed_qty"], ln["item_id"]),
        )


def _notify(conn, role, staff_id, order_id, text, action_required=False) -> None:
    """Queue a notification for a role / specific staffer."""
    conn.execute(
        "INSERT INTO notifications (role, staff_id, order_id, text,"
        " action_required, done, ts) VALUES (?,?,?,?,?,?,?)",
        (role, staff_id, order_id, text, int(action_required), 0, _now()),
    )


def _tell_customer(conn, order_id, text) -> None:
    """Outbound chat message to the order's customer (status updates)."""
    order = _get_order(conn, order_id)
    conn.execute(
        "INSERT INTO messages (customer_id, order_id, direction, text,"
        " audio_path, ts) VALUES (?,?, 'out', ?, NULL, ?)",
        (order["customer_id"], order_id, text, _now()),
    )


def _least_busy(conn, role, busy_states) -> int:
    """Staffer id with fewest live orders; lowest id breaks ties."""
    placeholders = ",".join("?" for _ in busy_states)
    row = conn.execute(
        "SELECT s.id FROM staff s LEFT JOIN orders o ON o."
        + ("packer_id" if role == "packer" else "delivery_id")
        + f" = s.id AND o.status IN ({placeholders})"
        " WHERE s.role = ? GROUP BY s.id ORDER BY COUNT(o.id), s.id LIMIT 1",
        (*busy_states, role),
    ).fetchone()
    if row is None:
        raise EngineError(f"No {role} on duty.")
    return int(row["id"])


def _delivery_fee(subtotal: float, rules: dict) -> float:
    """Flat fee below the free-delivery threshold, else 0."""
    return 0.0 if subtotal >= float(rules["delivery"]["free_above"]) else float(
        rules["delivery"]["fee"]
    )


# -- order intake ---------------------------------------------------------

def create_order_from_draft(
    customer_id: int,
    lines: list[dict],
    transcript: str = "",
    payment_mode: str | None = None,
    db_path: str | Path | None = None,
    rules: dict | None = None,
) -> int:
    """Price a draft, add the delivery fee, store it as NEW. Returns order id."""
    if not lines:
        raise EngineError("Draft has no lines.")
    rules = _rules(rules)
    conn = get_conn(db_path)
    try:
        if not conn.execute(
            "SELECT 1 FROM customers WHERE id = ?", (customer_id,)
        ).fetchone():
            raise EngineError(f"Customer #{customer_id} not found.")
        priced = []
        for ln in lines:
            item = conn.execute(
                "SELECT * FROM items WHERE id = ?", (ln["item_id"],)
            ).fetchone()
            if item is None:
                raise EngineError(f"Item #{ln['item_id']} not found.")
            if float(ln["qty"]) <= 0:
                raise EngineError(f"Qty for {item['name']} must be above 0.")
            priced.append((item, float(ln["qty"])))
        subtotal = sum(item["price"] * qty for item, qty in priced)
        fee = _delivery_fee(subtotal, rules)
        total = round(subtotal + fee, 2)
        ts = _now()
        cur = conn.execute(
            "INSERT INTO orders (customer_id, status, transcript, total,"
            " payment_mode, track_token, stage_entered_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (customer_id, NEW, transcript, total, payment_mode,
             secrets.token_urlsafe(16), ts, ts, ts),
        )
        order_id = cur.lastrowid
        for item, qty in priced:
            conn.execute(
                "INSERT INTO order_lines (order_id, item_id, qty, unit_price,"
                " packed_qty) VALUES (?,?,?, ?, 0)",
                (order_id, item["id"], qty, item["price"]),
            )
        fee_note = (
            "free delivery" if fee == 0 else f"incl. {rupees(fee)} delivery fee"
        )
        _log(conn, order_id, "engine", "created",
             f"Order #{order_id} drafted at {rupees(total)} ({fee_note}).",
             {"subtotal": subtotal, "delivery_fee": fee})
        conn.commit()
        return order_id
    finally:
        conn.close()


def apply_decision(
    order_id: int,
    decision: Decision,
    db_path: str | Path | None = None,
) -> str:
    """Move a NEW order to the state its Decision action maps to."""
    conn = get_conn(db_path)
    try:
        order = _get_order(conn, order_id)
        if order["status"] != NEW:
            raise InvalidTransition(
                f"Decision applies to NEW orders; #{order_id} is {order['status']}."
            )
        to_state = DECISION_STATE[decision.action]
        _move(conn, order_id, to_state, "engine", "decision",
              f"Rulebook says {decision.action}: " + "; ".join(decision.reasons),
              {"action": decision.action})
        if to_state == CONFIRMED:
            _reserve(conn, order_id)
            _log(conn, order_id, "engine", "reserve",
                 f"Stock reserved for order #{order_id}.")
        conn.commit()
        return to_state
    finally:
        conn.close()


# -- approvals ------------------------------------------------------------

def owner_approve(order_id: int, owner_id: int, db_path=None) -> str:
    """Owner clears a held order: reserve stock, mark CONFIRMED."""
    conn = get_conn(db_path)
    try:
        order = _get_order(conn, order_id)
        if order["status"] != NEEDS_OWNER_APPROVAL:
            raise InvalidTransition(
                f"Nothing awaiting the owner on order #{order_id}"
                f" ({order['status']})."
            )
        _move(conn, order_id, CONFIRMED, "owner", "approve",
              f"Owner approved order #{order_id}.", {"owner_id": owner_id})
        _reserve(conn, order_id)
        _log(conn, order_id, "engine", "reserve",
             f"Stock reserved for order #{order_id}.")
        conn.commit()
        return CONFIRMED
    finally:
        conn.close()


def owner_decline(order_id: int, owner_id: int, reason: str = "", db_path=None) -> str:
    """Owner rejects a held order. Nothing was reserved yet, nothing to release."""
    conn = get_conn(db_path)
    try:
        order = _get_order(conn, order_id)
        if order["status"] != NEEDS_OWNER_APPROVAL:
            raise InvalidTransition(
                f"Nothing awaiting the owner on order #{order_id}"
                f" ({order['status']})."
            )
        _move(conn, order_id, CANCELLED, "owner", "decline",
              f"Owner declined order #{order_id}."
              + (f" Reason: {reason}" if reason else ""),
              {"owner_id": owner_id})
        conn.commit()
        return CANCELLED
    finally:
        conn.close()


def customer_confirm(order_id: int, db_path=None) -> str:
    """Customer accepts the clarification (unusual qty / partial): CONFIRMED."""
    conn = get_conn(db_path)
    try:
        order = _get_order(conn, order_id)
        if order["status"] != NEEDS_CUSTOMER_CONFIRM:
            raise InvalidTransition(
                f"Nothing awaiting the customer on order #{order_id}"
                f" ({order['status']})."
            )
        _move(conn, order_id, CONFIRMED, "customer", "confirm",
              f"Customer confirmed order #{order_id}.")
        _reserve(conn, order_id)
        _log(conn, order_id, "engine", "reserve",
             f"Stock reserved for order #{order_id}.")
        conn.commit()
        return CONFIRMED
    finally:
        conn.close()


def cancel_order(order_id: int, actor: str = "owner", reason: str = "",
                 db_path=None) -> str:
    """Cancel a not-yet-dispatched order; reserved stock goes back on shelf."""
    conn = get_conn(db_path)
    try:
        order = _get_order(conn, order_id)
        if order["status"] in (CONFIRMED, READY_FOR_DELIVERY):
            _release(conn, order_id)
            _log(conn, order_id, "engine", "release",
                 f"Reserved stock released for order #{order_id}.")
        _move(conn, order_id, CANCELLED, actor, "cancel",
              f"Order #{order_id} cancelled."
              + (f" Reason: {reason}" if reason else ""))
        conn.commit()
        return CANCELLED
    finally:
        conn.close()


# -- packing --------------------------------------------------------------

def assign_staff(order_id: int, db_path=None) -> int:
    """Hand a CONFIRMED order to the least-busy packer. Returns packer id."""
    conn = get_conn(db_path)
    try:
        packer_id = _least_busy(conn, "packer", [PACKING])
        conn.execute(
            "UPDATE orders SET packer_id = ? WHERE id = ?", (packer_id, order_id)
        )
        _move(conn, order_id, PACKING, "engine", "assign",
              f"Order #{order_id} assigned to packer #{packer_id}.",
              {"packer_id": packer_id})
        _notify(conn, "packer", packer_id, order_id,
                f"Pack order #{order_id}.", action_required=True)
        _tell_customer(conn, order_id, _messages.status_packing_started())
        conn.commit()
        return packer_id
    finally:
        conn.close()


def submit_pack_counts(order_id: int, counts: dict[int, float],
                       db_path=None, rules: dict | None = None) -> str:
    """Packer reports packed qty per item.

    Clean pack -> READY_FOR_DELIVERY (delivery auto-assigned, least busy).
    Any line off by more than the tolerance -> PACK_MISMATCH, dispatch locked,
    owner AND packer notified.
    """
    rules = _rules(rules)
    tolerance = float(rules["packing"]["mismatch_tolerance"])
    conn = get_conn(db_path)
    try:
        order = _get_order(conn, order_id)
        if order["status"] != PACKING:
            raise InvalidTransition(
                f"Pack counts need a PACKING order; #{order_id} is {order['status']}."
            )
        lines = _lines(conn, order_id)
        if set(counts) != {ln["item_id"] for ln in lines}:
            raise EngineError("Pack counts must cover exactly the ordered items.")
        bad = []
        for ln in lines:
            packed = float(counts[ln["item_id"]])
            if packed < 0:
                raise EngineError(f"Packed qty for {ln['name']} cannot be negative.")
            conn.execute(
                "UPDATE order_lines SET packed_qty = ? WHERE id = ?",
                (packed, ln["id"]),
            )
            if abs(packed - ln["qty"]) > tolerance:
                bad.append(f"{ln['name']}: ordered {ln['qty']:g}, packed {packed:g}")
        if bad:
            _move(conn, order_id, PACK_MISMATCH, "packer", "mismatch",
                  f"Pack mismatch on order #{order_id} — " + "; ".join(bad)
                  + ". Dispatch locked.",
                  {"mismatches": bad})
            _notify(conn, "owner", None, order_id,
                    f"Pack mismatch on order #{order_id}: " + "; ".join(bad),
                    action_required=True)
            _notify(conn, "packer", order["packer_id"], order_id,
                    f"Recount needed on order #{order_id}.",
                    action_required=True)
            conn.commit()
            return PACK_MISMATCH
        delivery_id = _least_busy(conn, "delivery",
                                  [READY_FOR_DELIVERY, OUT_FOR_DELIVERY])
        conn.execute(
            "UPDATE orders SET delivery_id = ? WHERE id = ?",
            (delivery_id, order_id),
        )
        _move(conn, order_id, READY_FOR_DELIVERY, "packer", "packed",
              f"Order #{order_id} packed fully, with delivery #{delivery_id}.",
              {"delivery_id": delivery_id})
        _notify(conn, "delivery", delivery_id, order_id,
                f"Deliver order #{order_id}.", action_required=True)
        conn.commit()
        return READY_FOR_DELIVERY
    finally:
        conn.close()


def owner_resolve_mismatch(order_id: int, mode: str, owner_id: int,
                           db_path=None, rules: dict | None = None) -> str:
    """Owner clears a PACK_MISMATCH.

    accept_partial: bill what was actually packed (fee recomputed), rest
      goes back on shelf, order becomes deliverable.
    recount: back to PACKING for the packer to try again.
    cancel: whole order cancelled, reserved stock released.
    """
    if mode not in ("accept_partial", "recount", "cancel"):
        raise EngineError(f"Unknown mismatch mode: {mode}.")
    rules = _rules(rules)
    conn = get_conn(db_path)
    try:
        if mode == "recount":
            _move(conn, order_id, PACKING, "owner", "recount",
                  f"Owner asked for a recount on order #{order_id}.",
                  {"owner_id": owner_id})
            conn.commit()
            return PACKING
        if mode == "cancel":
            _release(conn, order_id)
            _log(conn, order_id, "engine", "release",
                 f"Reserved stock released for order #{order_id}.")
            _move(conn, order_id, CANCELLED, "owner", "cancel",
                  f"Owner cancelled mismatched order #{order_id}.",
                  {"owner_id": owner_id})
            conn.commit()
            return CANCELLED
        # accept_partial: unpacked qty returns to shelf; re-bill on packed qty.
        _adjust_to_packed(conn, order_id)
        subtotal = sum(
            ln["packed_qty"] * ln["unit_price"] for ln in _lines(conn, order_id)
        )
        fee = _delivery_fee(subtotal, rules)
        total = round(subtotal + fee, 2)
        conn.execute("UPDATE orders SET total = ? WHERE id = ?", (total, order_id))
        delivery_id = _least_busy(conn, "delivery",
                                  [READY_FOR_DELIVERY, OUT_FOR_DELIVERY])
        conn.execute(
            "UPDATE orders SET delivery_id = ? WHERE id = ?", (delivery_id, order_id)
        )
        _move(conn, order_id, READY_FOR_DELIVERY, "owner", "accept_partial",
              f"Owner accepted partial on order #{order_id}:"
              f" rebilled to {rupees(total)}.",
              {"owner_id": owner_id, "new_total": total})
        _notify(conn, "delivery", delivery_id, order_id,
                f"Deliver order #{order_id}.", action_required=True)
        conn.commit()
        return READY_FOR_DELIVERY
    finally:
        conn.close()


# -- delivery + payment ---------------------------------------------------

def start_delivery(order_id: int, delivery_id: int | None = None, db_path=None) -> str:
    """Rider leaves the shop with a READY order."""
    conn = get_conn(db_path)
    try:
        order = _get_order(conn, order_id)
        if order["status"] != READY_FOR_DELIVERY:
            raise InvalidTransition(
                f"Order #{order_id} is {order['status']}, not ready for delivery."
            )
        rider = delivery_id or order["delivery_id"]
        if rider is None:
            raise EngineError(f"Order #{order_id} has no delivery staffer.")
        conn.execute(
            "UPDATE orders SET delivery_id = ? WHERE id = ?", (rider, order_id)
        )
        _move(conn, order_id, OUT_FOR_DELIVERY, "delivery", "dispatch",
              f"Order #{order_id} out for delivery with #{rider}.",
              {"delivery_id": rider})
        eta_mins = int(_rules(None)["watchdog"]["delivery_max_minutes"])
        _tell_customer(conn, order_id,
                       _messages.status_out_for_delivery(f"~{eta_mins} min"))
        conn.commit()
        return OUT_FOR_DELIVERY
    finally:
        conn.close()


def mark_delivered(order_id: int, payment_mode: str, db_path=None) -> str:
    """Cash/UPI closes the order; credit adds the bill to the khata."""
    if payment_mode not in ("cash", "upi", "credit"):
        raise EngineError(f"Bad payment mode: {payment_mode}.")
    conn = get_conn(db_path)
    try:
        order = _get_order(conn, order_id)
        if order["status"] != OUT_FOR_DELIVERY:
            raise InvalidTransition(
                f"Order #{order_id} is {order['status']}, not out for delivery."
            )
        conn.execute(
            "UPDATE orders SET payment_mode = ? WHERE id = ?",
            (payment_mode, order_id),
        )
        _move(conn, order_id, DELIVERED, "delivery", "delivered",
              f"Order #{order_id} delivered, paid by {payment_mode}.",
              {"payment_mode": payment_mode})
        _tell_customer(conn, order_id, _messages.status_delivered(payment_mode))
        if payment_mode == "credit":
            conn.execute(
                "UPDATE customers SET outstanding = outstanding + ? WHERE id = ?",
                (order["total"], order["customer_id"]),
            )
            _log(conn, order_id, "engine", "khata",
                 f"{rupees(order['total'])} added to"
                 f" customer #{order['customer_id']}'s outstanding.")
        conn.commit()
        return DELIVERED
    finally:
        conn.close()


def report_delivery_problem(order_id: int, note: str,
                            delivery_id: int | None = None, db_path=None) -> str:
    """Rider flags a problem (locked shop, wrong address...): back to READY
    for a retry, owner notified."""
    conn = get_conn(db_path)
    try:
        _move(conn, order_id, READY_FOR_DELIVERY, "delivery", "problem",
              f"Delivery problem on order #{order_id}: {note} Held for retry.",
              {"delivery_id": delivery_id, "note": note})
        _notify(conn, "owner", None, order_id,
                f"Delivery problem on order #{order_id}: {note}",
                action_required=True)
        _tell_customer(conn, order_id, _messages.delivery_problem())
        conn.commit()
        return READY_FOR_DELIVERY
    finally:
        conn.close()
