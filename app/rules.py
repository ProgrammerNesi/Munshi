"""Deterministic rulebook: rules.yaml in, Decision out. No LLM here.

evaluate(order_draft, customer, stock) -> Decision(action, reasons)
  action is one of AUTO_CONFIRM, ASK_CUSTOMER, ASK_OWNER, REJECT_SHORT_STOCK.
  Every reason is a short human sentence with names and rupee amounts,
  suitable for showing the owner/customer directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RULES_PATH = ROOT / "rules.yaml"

# Decision actions. Priority order when several gates fire:
# REJECT_SHORT_STOCK > ASK_OWNER > ASK_CUSTOMER > AUTO_CONFIRM.
AUTO_CONFIRM = "AUTO_CONFIRM"
ASK_CUSTOMER = "ASK_CUSTOMER"
ASK_OWNER = "ASK_OWNER"
REJECT_SHORT_STOCK = "REJECT_SHORT_STOCK"


@dataclass
class Decision:
    """The rulebook's verdict on one order draft."""

    action: str
    reasons: list[str] = field(default_factory=list)


def load_rules(path: str | Path | None = None) -> dict:
    """Load rules.yaml. Explicit path wins, else the repo-root file."""
    with open(Path(path) if path else DEFAULT_RULES_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def rupees(amount: float) -> str:
    """Format a rupee amount for human sentences: 24300 -> ₹24,300."""
    return f"₹{amount:,.0f}"


def _usual_qty_by_item(customer: dict) -> dict[str, float]:
    """Customer's usual basket as {item name: qty} (names as in items table)."""
    basket = customer.get("usual_basket") or customer.get("usual_basket_json") or []
    if isinstance(basket, str):  # tolerate a raw JSON string from the DB row
        import json

        basket = json.loads(basket)
    return {entry["item"]: float(entry["qty"]) for entry in basket}


def evaluate(
    order_draft: dict,
    customer: dict,
    stock: dict,
    rules: dict | None = None,
) -> Decision:
    """Run every gate; return the strictest action with human reasons.

    order_draft: {"lines": [{"item_id": int, "qty": number}]}.
    customer: {"name", "credit_limit", "outstanding", "is_new", "usual_basket"}.
    stock: {item_id: {"name", "price", "stock_qty"}}.
    """
    rules = rules if rules is not None else load_rules()
    lines = order_draft.get("lines", [])

    # Price each line from the shelf snapshot (single source of truth).
    priced = []  # (item_id, name, qty, unit_price, line_total)
    for line in lines:
        item = stock[line["item_id"]]
        qty = float(line["qty"])
        priced.append((line["item_id"], item["name"], qty,
                       float(item["price"]), qty * float(item["price"])))
    total = sum(p[4] for p in priced)

    name = customer.get("name", "Customer")
    reasons: list[str] = []
    action = AUTO_CONFIRM

    def escalate(to: str, reason: str) -> None:
        """Raise the action only towards stricter outcomes; always keep reason."""
        nonlocal action
        nonlocal reasons
        order = [AUTO_CONFIRM, ASK_CUSTOMER, ASK_OWNER, REJECT_SHORT_STOCK]
        reasons.append(reason)
        if order.index(to) > order.index(action):
            action = to

    # Gate 1: new customers always need the owner's eyes first.
    if rules["customers"]["new_customer_requires_approval"] and customer.get("is_new"):
        escalate(ASK_OWNER, f"{name} is a new customer, owner approval needed.")

    # Gate 2: short stock. Default policy rejects; partials only if enabled.
    allow_partial = rules["stock"]["allow_partial_when_short"]
    for _id, item_name, qty, _price, _lt in priced:
        on_hand = float(stock[_id]["stock_qty"])
        if qty > on_hand:
            if not allow_partial or on_hand <= 0:
                escalate(
                    REJECT_SHORT_STOCK,
                    f"Only {on_hand:g} {item_name} in stock, order needs {qty:g}.",
                )
            else:
                escalate(
                    ASK_CUSTOMER,
                    f"Only {on_hand:g} {item_name} in stock"
                    f" (order needs {qty:g}) — confirm a partial?",
                )

    # Gate 3: khata (credit) limit on outstanding + this order.
    if rules["credit"]["require_owner_approval_above_outstanding_plus_order"]:
        limit = float(customer.get("credit_limit", 0))
        due = float(customer.get("outstanding", 0)) + total
        if due > limit:
            escalate(
                ASK_OWNER,
                f"Order of {rupees(total)} would put {name} at {rupees(due)}"
                f" against a {rupees(limit)} credit limit.",
            )

    # Gate 4: big-ticket orders need the owner even when credit is fine.
    max_auto = float(rules["order"]["auto_confirm_max_amount"])
    if total > max_auto:
        escalate(
            ASK_OWNER,
            f"Order of {rupees(total)} is above the {rupees(max_auto)}"
            " auto-confirm limit.",
        )

    # Gate 5: unusual quantity vs this customer's own usual basket.
    mult = float(rules["order"]["unusual_qty_multiplier"])
    usual = _usual_qty_by_item(customer)
    for _id, item_name, qty, _price, _lt in priced:
        if item_name in usual and usual[item_name] > 0 and qty > usual[item_name] * mult:
            escalate(
                ASK_CUSTOMER,
                f"{qty:g} {item_name} is more than {mult:g}x your usual"
                f" {usual[item_name]:g} — please confirm.",
            )

    if action == AUTO_CONFIRM:
        reasons.append(
            f"Routine order of {rupees(total)} for {name}:"
            " within credit, stock and quantity limits."
        )
    return Decision(action=action, reasons=reasons)
