"""Deterministic orchestration: message -> heard -> understood -> checked.

decide() -> act. One queue, one background worker thread (FIFO, so order is
preserved per customer). Every step writes an events row with seconds.

Pipeline-level statuses (engine.py is untouched; it owns CONFIRMED onwards):
  NEW -> CLARIFYING -> NEW ... (reply parsed, draft updated, decide re-runs)
  NEW -> AWAITING_APPROVAL -> CONFIRMED (owner_approve) | CANCELLED (decline)
"""

from __future__ import annotations

import json
import secrets
import queue
import re
import threading
import time
from pathlib import Path

from app import bill as bill_mod
from app import engine, messages
from app import rules as rules_mod
from app.ai import extract as ai_extract
from app.ai import stt
from app.ai.audio import AudioError
from app.ai.extract import UNRESOLVED
from app.ai.stt import AudioBackendUnavailable
from app import clock
from app.db import get_conn
from app.rules import Decision

# Pipeline-owned order statuses (pre-engine).
CLARIFYING = "CLARIFYING"
AWAITING_APPROVAL = "AWAITING_APPROVAL"

_YES = {"haan", "han", "yes", "ok", "confirm", "sahi", "bhej do", "theek"}
_NO = {"nahi", "na", "no", "nahin", "cancel", "rehne do", "mat bhejo"}


def _now() -> str:
    """Demo-aware timestamp for ts / updated_at columns."""
    return clock.now().isoformat()


def _ensure_schema(conn) -> None:
    """pending_json column for orders (ALTER on old DBs; fresh ones have it)."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(orders)")]
    if "pending_json" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN pending_json TEXT")


def _log(conn, order_id, actor, kind, message, seconds, data=None) -> None:
    """One reasoning-log row, always carrying the step's seconds."""
    conn.execute(
        "INSERT INTO events (order_id, actor, kind, message, data_json, ts)"
        " VALUES (?,?,?,?,?,?)",
        (order_id, actor, kind, message,
         json.dumps({**(data or {}), "seconds": round(seconds, 2)}), _now()),
    )


def _send(conn, customer_id, order_id, text) -> int:
    """Store an outbound (Munshi -> customer) chat message."""
    cur = conn.execute(
        "INSERT INTO messages (customer_id, order_id, direction, text,"
        " audio_path, ts) VALUES (?,?,?,?,?,?)",
        (customer_id, order_id, "out", text, None, _now()),
    )
    return cur.lastrowid


def _notify_owner(conn, order_id, text) -> None:
    """Flag the owner (action required) about one order."""
    owner = conn.execute(
        "SELECT id FROM staff WHERE role = 'owner' ORDER BY id LIMIT 1"
    ).fetchone()
    conn.execute(
        "INSERT INTO notifications (role, staff_id, order_id, text,"
        " action_required, done, ts) VALUES (?,?,?,?,?,?,?)",
        ("owner", owner["id"] if owner else None, order_id, text, 1, 0, _now()),
    )


def _context(conn, customer_id):
    """Customer + stock dicts shaped for rules.evaluate, plus catalog terms."""
    c = conn.execute(
        "SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    basket = json.loads(c["usual_basket_json"])
    customer = {
        "name": c["name"], "credit_limit": c["credit_limit"],
        "outstanding": c["outstanding"], "is_new": c["is_new"],
        "usual_basket": basket,
    }
    stock, terms = {}, []
    for r in conn.execute("SELECT * FROM items"):
        stock[r["id"]] = {"name": r["name"], "price": r["price"],
                          "stock_qty": r["stock_qty"]}
        terms.append(r["name"])
        terms.extend(json.loads(r["aliases_json"]))
    return customer, stock, [e["item"] for e in basket], terms


def _rewrite_lines(conn, order_id, rule_lines, rules) -> float:
    """Replace order_lines from resolved draft lines; recompute + store total."""
    conn.execute("DELETE FROM order_lines WHERE order_id = ?", (order_id,))
    subtotal = 0.0
    for ln in rule_lines:
        item = conn.execute(
            "SELECT price FROM items WHERE id = ?", (ln["item_id"],)).fetchone()
        conn.execute(
            "INSERT INTO order_lines (order_id, item_id, qty, unit_price,"
            " packed_qty) VALUES (?,?,?,?,0)",
            (order_id, ln["item_id"], ln["qty"], item["price"]),
        )
        subtotal += item["price"] * ln["qty"]
    fee = 0.0 if subtotal >= float(rules["delivery"]["free_above"]) \
        else float(rules["delivery"]["fee"])
    total = round(subtotal + fee, 2)
    conn.execute(
        "UPDATE orders SET total = ?, updated_at = ? WHERE id = ?",
        (total, _now(), order_id),
    )
    return total


def _reserve_stock(conn, order_id) -> None:
    """Decrement shelf stock by ordered qty (owner-approve confirm path)."""
    for ln in conn.execute(
            "SELECT ol.qty, ol.item_id, i.name, i.stock_qty FROM order_lines ol"
            " JOIN items i ON i.id = ol.item_id WHERE ol.order_id = ?",
            (order_id,)):
        if ln["stock_qty"] < ln["qty"]:
            raise engine.EngineError(
                f"Cannot reserve {ln['qty']:g} {ln['name']}.")
        conn.execute("UPDATE items SET stock_qty = stock_qty - ? WHERE id = ?",
                     (ln["qty"], ln["item_id"]))


# -- decide -------------------------------------------------------------------

def _agent_enabled() -> bool:
    """Agent loop on/off (MUNSHI_AGENT, default 1). Off = deterministic path."""
    import os

    return os.environ.get("MUNSHI_AGENT", "1") == "1"


_AGENT_ACTION = {"confirm": rules_mod.AUTO_CONFIRM,
                 "ask_customer": rules_mod.ASK_CUSTOMER,
                 "ask_owner": rules_mod.ASK_OWNER,
                 "reject_short_stock": rules_mod.REJECT_SHORT_STOCK}


def decide(order_id, rule_lines, customer, stock,
           db_path=None, rules=None, draft_lines=None) -> Decision:
    """Run the rulebook and log the verdict with seconds.

    With MUNSHI_AGENT=1 (default) the agent loop investigates first, but the
    rulebook reasons below stay canonical and the engine commits every action.
    """
    from app import agent as agent_mod  # local: agent must never import pipeline

    t0 = time.perf_counter()
    rules = rules if rules is not None else rules_mod.load_rules()
    ask_text = ""
    if _agent_enabled():
        res = agent_mod.run_agent(order_id, draft_lines=draft_lines,
                                  db_path=db_path, rules=rules)
        action = _AGENT_ACTION[res.kind]
        if res.kind == "ask_customer":
            ask_text = res.text
        agent_note = (f"Agent: {res.kind} in {res.seconds:.1f}s"
                      f" over {len(res.tool_calls)} tool call(s)"
                      + (" (fallback: " + res.fallback_reason + ")"
                         if res.used_fallback else "") + ". ")
    else:
        action = None
        agent_note = ""
    decision = rules_mod.evaluate({"lines": rule_lines}, customer, stock, rules)
    if _agent_enabled():
        decision = Decision(action, reasons=decision.reasons, ask_text=ask_text)
    seconds = time.perf_counter() - t0
    conn = get_conn(db_path)
    try:
        _log(conn, order_id, "munshi", "decision",
             agent_note + f"Rulebook says {decision.action}: "
             + "; ".join(decision.reasons),
             seconds, {"action": decision.action})
        conn.commit()
    finally:
        conn.close()
    return decision


# -- clarification replies -----------------------------------------------------

def _resolve_clarification(reply: str, pending: dict, catalog: list[dict]):
    """Parse a CLARIFYING reply. Returns (draft_lines, ok).

    Accepts a candidate number ("1"/"2"), an item name (extracted, must be
    one of the candidates), or a fresh quantity for the pending line.
    """
    draft, idx = [dict(ln) for ln in pending["draft"]], pending["line_index"]
    line = draft[idx]
    kind = pending.get("type", "pick_item")

    if kind == "confirm_qty":
        words = set(re.findall(r"[a-z]+", reply.lower()))
        if words & _YES:
            return draft, True
        if words & _NO:
            return draft, False
        nums = re.findall(r"\d+\.?\d*", reply)
        if nums:  # new weight offered: update the draft, re-decide later
            line["qty"] = float(nums[0])
            return draft, True
        return draft, None

    # pick_item / ask_qty: numeric reply first (no LLM needed). With
    # candidates it picks one; without, a bare number is the weight.
    if re.fullmatch(r"\d+\.?\d*", reply.strip()):
        n = float(reply.strip())
        if pending.get("candidates"):
            if 1 <= int(n) <= len(pending["candidates"]) and n.is_integer():
                name = pending["candidates"][int(n) - 1]
                entry = next(e for e in catalog if e["name"] == name)
                line.update(item_id=entry["item_id"], item_name=name, status="ok")
                return draft, True
            return draft, None
        line["qty"] = n
        return draft, True

    from app.ai.extract import extract_order  # local: keeps import graph flat

    # Extraction with the candidates as hints (usual-basket tie-break input).
    small = [e for e in catalog if e["name"] in pending.get("candidates", [])]
    got = extract_order(reply, usual_names=pending.get("candidates", []),
                        catalog=catalog or small)
    if got.lines:
        first = got.lines[0]
        if first.item_id is not None and first.item_name in pending.get(
                "candidates", []):
            line.update(item_id=first.item_id, item_name=first.item_name,
                        status="ok")
            if first.qty is not None:
                line["qty"] = first.qty
            return draft, True
        if first.qty is not None and line.get("qty") is None:
            line["qty"] = first.qty  # quantity supplied; item still open
    return draft, None


def _pending_question(pending: dict) -> str:
    """Template question for the first open draft line."""
    line = pending["draft"][pending["line_index"]]
    if pending.get("type") == "confirm_qty":
        usual = pending.get("usual", 0)
        return messages.confirm_unusual_qty(line["item_name"], line["qty"], usual)
    if line.get("qty") is None:
        return messages.clarify_qty(line.get("item_name") or line["raw"])
    return messages.clarify_item(line["raw"], pending.get("candidates", []))


# -- main entry ------------------------------------------------------------------

def process_message_now(message_id: int, db_path=None):
    """Run one message through heard/understood/checked/decide/act. Never raises."""
    conn = get_conn(db_path)
    customer_id = None
    try:
        _ensure_schema(conn)
        msg = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if msg is None:
            return None
        customer_id = msg["customer_id"]
        clar = conn.execute(
            "SELECT * FROM orders WHERE customer_id = ? AND status = ?"
            " ORDER BY id DESC LIMIT 1",
            (customer_id, CLARIFYING)).fetchone()
        if clar is not None:
            oid = _continue_clarification(conn, clar, msg, db_path)
        else:
            oid = _fresh_order(conn, msg, db_path)
        conn.commit()
        return oid
    except Exception as e:  # never crash, never strand silently
        conn.rollback()
        try:
            _log(conn, None, "munshi", "error",
                 f"Pipeline failed on message #{message_id}: {e}.", 0.0)
            # Customer still hears back: fall back to a typed retry.
            oid = conn.execute(
                "SELECT id, customer_id FROM orders WHERE id IN"
                " (SELECT order_id FROM messages WHERE id = ?)",
                (message_id,)).fetchone()
            if oid is not None:
                _send(conn, oid["customer_id"], oid["id"],
                      messages.could_not_understand())
            elif customer_id is not None:
                _send(conn, customer_id, None, messages.could_not_understand())
            conn.commit()
        except Exception:
            pass
        return None
    finally:
        conn.close()


def _heard_text(conn, msg, terms) -> tuple[str, float]:
    """Transcript + STT seconds (text messages cost 0)."""
    if not msg["audio_path"]:
        return msg["text"] or "", 0.0
    t0 = time.perf_counter()
    try:
        res = stt.transcribe(msg["audio_path"], catalog_terms=terms)
    except (AudioBackendUnavailable, AudioError) as e:
        raise _Understandable(f"stt failed: {e}")
    return res.text, time.perf_counter() - t0


class _Understandable(Exception):
    """Expected failure (STT/extract): customer gets could_not_understand."""


def _fail_gracefully(conn, order_id, customer_id, message_id, why) -> None:
    """could_not_understand to the customer; order stays NEW; event logged."""
    _log(conn, order_id, "munshi", "understood" if "llm" in why else "heard",
         f"Could not understand message #{message_id or '?'} ({why}).", 0.0)
    _send(conn, customer_id, order_id, messages.could_not_understand())


def _fresh_order(conn, msg, db_path):
    """Intake: blank NEW order, then the five steps."""
    customer_id = msg["customer_id"]
    ts = _now()
    oid = conn.execute(
        "INSERT INTO orders (customer_id, status, transcript, total,"
        " payment_mode, track_token, stage_entered_at,"
        " created_at, updated_at) VALUES (?,?,?,0,NULL,?,?,?,?)",
        (customer_id, engine.NEW, "", secrets.token_urlsafe(16), ts, ts, ts),
    ).lastrowid
    conn.execute("UPDATE messages SET order_id = ? WHERE id = ?", (oid, msg["id"]))
    customer, stock, usual_names, terms = _context(conn, customer_id)

    # heard
    try:
        transcript, stt_s = _heard_text(conn, msg, terms)
    except _Understandable as e:
        _fail_gracefully(conn, oid, customer_id, msg["id"], str(e))
        return oid
    conn.execute("UPDATE orders SET transcript = ? WHERE id = ?", (transcript, oid))
    _log(conn, oid, "munshi", "heard", f"Heard: {transcript[:120]}.", stt_s)

    # understood
    t0 = time.perf_counter()
    draft = ai_extract.extract_order(transcript, usual_names=usual_names,
                                     db_path=db_path)
    _log(conn, oid, "munshi", "understood",
         f"Understood {len(draft.lines)} line(s).", time.perf_counter() - t0,
         {"needs_retype": draft.needs_retype})
    if draft.needs_retype or not draft.lines:
        _fail_gracefully(conn, oid, customer_id, msg["id"], "llm no lines")
        return oid

    rules = rules_mod.load_rules()
    rule_lines = [{"item_id": ln.item_id, "qty": ln.qty} for ln in draft.lines
                  if ln.item_id is not None and ln.qty is not None]
    _rewrite_lines(conn, oid, rule_lines, rules)

    # unresolved item? ask before anything else touches money/stock.
    open_idx = next((i for i, ln in enumerate(draft.lines)
                     if ln.status == UNRESOLVED or ln.qty is None), None)
    if open_idx is not None:
        return _ask(conn, oid, customer_id, draft, open_idx, db_path)

    # checked: snapshot the facts the rulebook is about to judge.
    t0 = time.perf_counter()
    facts = {"outstanding": customer["outstanding"],
             "credit_limit": customer["credit_limit"],
             "stock": {stock[ln["item_id"]]["name"]: stock[ln["item_id"]]["stock_qty"]
                       for ln in rule_lines}}
    _log(conn, oid, "munshi", "checked",
         f"Checked credit {customer['outstanding']:g}/{customer['credit_limit']:g}"
         f" and stock for {len(rule_lines)} line(s).",
         time.perf_counter() - t0, facts)
    # Commit before decide/act: engine.* and decide() use their own
    # connections, and SQLite forbids overlapping write transactions.
    conn.commit()

    decision = decide(oid, rule_lines, customer, stock, db_path, rules,
                      draft_lines=[ln.to_dict() for ln in draft.lines])
    return _act(conn, oid, customer_id, decision, rule_lines, customer, stock,
                db_path, rules)


def _ask(conn, oid, customer_id, draft, open_idx, db_path) -> int:
    """Store the pending question on the order; status CLARIFYING."""
    line = draft.lines[open_idx]
    catalog = ai_extract.load_catalog(db_path)
    if line.qty is None and line.item_id is not None:
        pending = {"type": "ask_qty",
                   "draft": [ln.to_dict() for ln in draft.lines],
                   "line_index": open_idx, "candidates": []}
    elif line.status == UNRESOLVED:
        cands = line.candidates or [e["name"] for e, _ in sorted(
            ((e, ai_extract._score_guess(
                ai_extract.normalise(line.item_name), e)) for e in catalog),
            key=lambda p: p[1], reverse=True)[:3]]
        pending = {"type": "pick_item",
                   "draft": [ln.to_dict() for ln in draft.lines],
                   "line_index": open_idx, "candidates": cands}
    else:  # shouldn't happen; fail loud in the log, polite to customer
        _fail_gracefully(conn, oid, customer_id, 0, "llm bad line state")
        return oid
    conn.execute(
        "UPDATE orders SET status = ?, pending_json = ?, updated_at = ?"
        " WHERE id = ?", (CLARIFYING, json.dumps(pending), _now(), oid))
    _log(conn, oid, "munshi", "act",
         f"Asking customer: {pending['type']} on line {open_idx + 1}.", 0.0)
    _send(conn, customer_id, oid, _pending_question(pending))
    return oid


def _continue_clarification(conn, clar, msg, db_path):
    """Reply to a CLARIFYING order: resolve, update draft, re-run decide."""
    oid, customer_id = clar["id"], clar["customer_id"]
    pending = json.loads(clar["pending_json"] or "{}")
    customer, stock, usual_names, terms = _context(conn, customer_id)
    try:
        reply, stt_s = _heard_text(conn, msg, terms)
    except _Understandable as e:
        _fail_gracefully(conn, oid, customer_id, msg["id"], str(e))
        return oid
    conn.execute("UPDATE messages SET order_id = ? WHERE id = ?", (oid, msg["id"]))
    _log(conn, oid, "munshi", "heard", f"Heard reply: {reply[:120]}.", stt_s)

    catalog = ai_extract.load_catalog(db_path)
    t0 = time.perf_counter()
    old_qty = (pending.get("draft") or [{}])[pending["line_index"]].get("qty")
    draft_dicts, verdict = _resolve_clarification(reply, pending, catalog)
    _log(conn, oid, "munshi", "understood",
         f"Reply parsed: {'resolved' if verdict else 'still unclear'}.",
         time.perf_counter() - t0)
    # Commit before decide/act (same SQLite reason as in _fresh_order).
    conn.commit()
    if verdict is False:  # explicit no: cancel politely
        conn.execute(
            "UPDATE orders SET status = ?, pending_json = NULL,"
            " updated_at = ? WHERE id = ?",
            (engine.CANCELLED, _now(), oid))
        _log(conn, oid, "munshi", "act", "Customer declined; cancelled.", 0.0)
        _send(conn, customer_id, oid, messages.order_cancelled())
        return oid
    if verdict is None:  # still unclear: ask again, keep the same pending
        _send(conn, customer_id, oid, _pending_question(pending))
        return oid
    if (pending.get("type") == "confirm_qty"
            and draft_dicts[pending["line_index"]].get("qty") == old_qty):
        # Plain HAAN: re-deciding would loop, so confirm outright.
        return _confirm_auto(
            conn, oid, customer_id,
            Decision(rules_mod.AUTO_CONFIRM,
                     [f"Customer confirmed {old_qty:g} "
                      f"{draft_dicts[pending['line_index']].get('item_name')}."]),
            db_path, rules_mod.load_rules())

    # Resolved: new draft -> fresh lines -> checked/decide/act.
    open_idx = next((i for i, ln in enumerate(draft_dicts)
                     if ln.get("item_id") is None or ln.get("qty") is None), None)
    if open_idx is not None:
        still_open = {"type": "pick_item", "draft": draft_dicts,
                      "line_index": open_idx,
                      "candidates": draft_dicts[open_idx].get("candidates", [])}
        conn.execute("UPDATE orders SET pending_json = ? WHERE id = ?",
                     (json.dumps(still_open), oid))
        _send(conn, customer_id, oid, _pending_question(still_open))
        return oid

    conn.execute("UPDATE orders SET status = ?, pending_json = NULL,"
                 " updated_at = ? WHERE id = ?", (engine.NEW, _now(), oid))
    rules = rules_mod.load_rules()
    rule_lines = [{"item_id": ln["item_id"], "qty": ln["qty"]} for ln in draft_dicts
                  if ln.get("item_id") is not None and ln.get("qty") is not None]
    _rewrite_lines(conn, oid, rule_lines, rules)
    conn.commit()  # engine/decide use their own connections (see above)
    decision = decide(oid, rule_lines, customer, stock, db_path, rules,
                      draft_lines=draft_dicts)
    return _act(conn, oid, customer_id, decision, rule_lines, customer, stock,
                db_path, rules)


def _confirm_auto(conn, oid, customer_id, decision, db_path, rules) -> int:
    """AUTO path: engine confirms + reserves, packer assigned, bill sent."""
    t0 = time.perf_counter()
    engine.apply_decision(oid, decision, db_path)
    engine.assign_staff(oid, db_path)
    text = bill_mod.render_text(bill_mod.build_bill(oid, db_path, rules))
    _send(conn, customer_id, oid, text)
    _log(conn, oid, "munshi", "act",
         "Confirmed, stock reserved, packer assigned, bill sent.",
         time.perf_counter() - t0)
    return oid


def _act(conn, oid, customer_id, decision, rule_lines, customer, stock,
         db_path, rules) -> int:
    """Carry out the Decision: confirm / clarify / hold / reject."""
    t0 = time.perf_counter()
    action = decision.action
    if action == rules_mod.AUTO_CONFIRM:
        return _confirm_auto(conn, oid, customer_id, decision, db_path, rules)
    if action == rules_mod.ASK_CUSTOMER:
        # Unusual quantity vs the customer's own basket: confirm explicitly.
        first = rule_lines[0]
        usual = {e["item"]: e["qty"] for e in customer["usual_basket"]}
        name = stock[first["item_id"]]["name"]
        pending = {"type": "confirm_qty",
                   "draft": [{"raw": name, "item_id": first["item_id"],
                              "item_name": name, "qty": first["qty"],
                              "unit": stock[first["item_id"]].get("unit", ""),
                              "score": 100.0, "status": "ok", "candidates": []}],
                   "line_index": 0, "candidates": [],
                   "usual": usual.get(name, 0)}
        conn.execute(
            "UPDATE orders SET status = ?, pending_json = ?, updated_at = ?"
            " WHERE id = ?", (CLARIFYING, json.dumps(pending), _now(), oid))
        _send(conn, customer_id, oid,
              decision.ask_text or _pending_question(pending))
        _log(conn, oid, "munshi", "act", "Asking customer to confirm qty.",
             time.perf_counter() - t0)
        return oid
    if action == rules_mod.ASK_OWNER:
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (AWAITING_APPROVAL, _now(), oid))
        _send(conn, customer_id, oid, messages.awaiting_owner())
        _notify_owner(conn, oid, f"Order #{oid} needs approval: "
                      + "; ".join(decision.reasons))
        _log(conn, oid, "munshi", "act", "Held for owner approval.",
             time.perf_counter() - t0)
        return oid
    # REJECT_SHORT_STOCK: name the shortage, offer what exists.
    engine.apply_decision(oid, decision, db_path)
    short = next((ln for ln in rule_lines
                  if ln["qty"] > stock[ln["item_id"]]["stock_qty"]), rule_lines[0])
    _send(conn, customer_id, oid, messages.short_stock(
        stock[short["item_id"]]["name"], stock[short["item_id"]]["stock_qty"]))
    _log(conn, oid, "munshi", "act", "Rejected: short stock, customer told.",
         time.perf_counter() - t0)
    return oid


# -- owner actions (called by the owner UI; a later phase wires the buttons)

def owner_approve(order_id: int, owner_id: int, db_path=None) -> str:
    """Owner clears an AWAITING_APPROVAL order: same path as AUTO_CONFIRM."""
    conn = get_conn(db_path)
    try:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None or order["status"] != AWAITING_APPROVAL:
            raise engine.EngineError(
                f"Order #{order_id} is not awaiting approval.")
        _reserve_stock(conn, order_id)
        conn.execute(
            "UPDATE orders SET status = ?, pending_json = NULL, updated_at = ?"
            " WHERE id = ?", (engine.CONFIRMED, _now(), order_id))
        _log(conn, order_id, "owner", "approve",
             f"Owner approved order #{order_id}.", 0.0, {"owner_id": owner_id})
        _log(conn, order_id, "engine", "reserve",
             f"Stock reserved for order #{order_id}.", 0.0)
        conn.commit()
    finally:
        conn.close()
    engine.assign_staff(order_id, db_path)
    conn = get_conn(db_path)
    try:
        rules = rules_mod.load_rules()
        _send(conn, order["customer_id"], order_id,
              bill_mod.render_text(bill_mod.build_bill(order_id, db_path, rules)))
        conn.commit()
    finally:
        conn.close()
    return engine.CONFIRMED


def owner_decline(order_id: int, owner_id: int, reason: str = "", db_path=None) -> str:
    """Owner rejects an AWAITING_APPROVAL order: polite word to the customer."""
    conn = get_conn(db_path)
    try:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None or order["status"] != AWAITING_APPROVAL:
            raise engine.EngineError(
                f"Order #{order_id} is not awaiting approval.")
        conn.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (engine.CANCELLED, _now(), order_id))
        _log(conn, order_id, "owner", "decline",
             f"Owner declined order #{order_id}."
             + (f" Reason: {reason}" if reason else ""), 0.0,
             {"owner_id": owner_id})
        _send(conn, order["customer_id"], order_id, messages.owner_declined())
        conn.commit()
    finally:
        conn.close()
    return engine.CANCELLED


# -- activity label (for GET /api/state) -----------------------------------------

def inflight_activity(customer_id: int, db_path=None) -> str | None:
    """Current phase label from the in-flight order's latest event."""
    conn = get_conn(db_path)
    try:
        order = conn.execute(
            "SELECT * FROM orders WHERE customer_id = ? AND UPPER(status) NOT IN"
            " ('DELIVERED','CANCELLED','REJECTED') ORDER BY id DESC LIMIT 1",
            (customer_id,)).fetchone()
        if order is None:
            return None
        ev = conn.execute(
            "SELECT kind FROM events WHERE order_id = ? ORDER BY id DESC LIMIT 1",
            (order["id"],)).fetchone()
        kind = ev["kind"] if ev else ""
        status = order["status"]
        if status == CLARIFYING:
            return "waiting for customer"
        if status == AWAITING_APPROVAL:
            return "waiting for owner"
        if status in (engine.CONFIRMED, engine.PACKING):
            return "packing"
        if status == engine.READY_FOR_DELIVERY:
            return "ready for delivery"
        if status == engine.OUT_FOR_DELIVERY:
            return "out for delivery"
        return {"heard": "listening", "understood": "understanding",
                "checked": "checking stock", "decision": "checking stock"}.get(
                    kind, "working on it")
    finally:
        conn.close()


# -- background worker: one queue, one thread, FIFO per customer ------------------

_queue: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None
_lock = threading.Lock()


def queue_depth() -> int:
    """Unprocessed messages waiting in the worker queue."""
    return _queue.qsize()


def enqueue(message_id: int, db_path=None) -> int:
    """Queue a message for the background worker. Returns queue depth."""
    ensure_worker()
    _queue.put((message_id, db_path))
    return queue_depth()


def ensure_worker() -> None:
    """Start the single daemon worker thread (idempotent)."""
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, daemon=True,
                                       name="munshi-pipeline")
            _worker.start()


def _loop() -> None:
    """Worker body: one message at a time, never let an error kill the loop."""
    while True:
        message_id, db_path = _queue.get()
        try:
            process_message_now(message_id, db_path)
        except Exception:
            pass  # process_message_now already logged; stay alive regardless
        finally:
            _queue.task_done()
