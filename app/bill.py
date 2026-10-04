"""Deterministic billing: DB rows + rules.yaml in, bill dict + text out.

No LLM in the money path. The bill intro may use LLM wording only through
bill_intro(), which rejects any text containing a number not present in
the bill itself (so the model can never invent a discount or total).
"""

from __future__ import annotations

import re
from pathlib import Path

from app import messages
from app.db import get_conn
from app.rules import load_rules, rupees


def build_bill(order_id: int, db_path: str | Path | None = None,
               rules: dict | None = None) -> dict:
    """Bill dict for one order: lines, fee from rules, total, ETA text."""
    rules = rules if rules is not None else load_rules()
    conn = get_conn(db_path)
    try:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise ValueError(f"Order #{order_id} not found.")
        rows = conn.execute(
            "SELECT ol.qty, ol.unit_price, i.name, i.unit FROM order_lines ol"
            " JOIN items i ON i.id = ol.item_id WHERE ol.order_id = ?"
            " ORDER BY ol.id",
            (order_id,),
        ).fetchall()
        lines = [
            {"name": r["name"], "qty": r["qty"], "unit": r["unit"],
             "rate": r["unit_price"],
             "line_total": round(r["qty"] * r["unit_price"], 2)}
            for r in rows
        ]
        subtotal = round(sum(ln["line_total"] for ln in lines), 2)
        fee = 0.0 if subtotal >= float(rules["delivery"]["free_above"]) \
            else float(rules["delivery"]["fee"])
        eta_mins = int(rules["watchdog"]["delivery_max_minutes"])
        return {
            "order_id": order_id,
            "lines": lines,
            "subtotal": subtotal,
            "delivery_fee": fee,
            "total": round(subtotal + fee, 2),
            "payment_mode": order["payment_mode"],
            "eta_text": f"Delivery within ~{eta_mins} minutes",
        }
    finally:
        conn.close()


def render_text(bill: dict, tracking_url: str = "") -> str:
    """Plain-text bill for the chat bubble (same numbers as the dict)."""
    parts = [messages.bill_intro()]
    for ln in bill["lines"]:
        parts.append(
            f"{ln['name']}: {ln['qty']:g} {ln['unit']} x"
            f" {rupees(ln['rate'])} = {rupees(ln['line_total'])}"
        )
    parts.append(f"Delivery: {rupees(bill['delivery_fee'])}")
    parts.append(f"Total: {rupees(bill['total'])}")
    parts.append(bill["eta_text"])
    if tracking_url:
        parts.append(f"Track order: {tracking_url}")
    return "\n".join(parts)


def _numbers(text: str) -> list[str]:
    """Digit runs, commas stripped: '₹24,300' -> ['24300']."""
    return [n.replace(",", "") for n in re.findall(r"\d[\d,]*\.?\d*", text)]


def bill_intro(bill: dict, llm_text: str | None = None) -> str:
    """Friendly bill header. LLM wording only if it invents no numbers."""
    if llm_text:
        rendered = render_text(bill).replace(",", "")
        if all(n in rendered for n in _numbers(llm_text)):
            return llm_text
    return messages.bill_intro()
