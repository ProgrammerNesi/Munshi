"""Unit tests for the deterministic rulebook (no DB needed)."""

from app.rules import (
    ASK_CUSTOMER,
    ASK_OWNER,
    AUTO_CONFIRM,
    REJECT_SHORT_STOCK,
    evaluate,
    load_rules,
)

STOCK = {
    1: {"name": "Sugar", "price": 45.0, "stock_qty": 500},
    2: {"name": "Wheat Flour (Atta)", "price": 42.0, "stock_qty": 400},
    3: {"name": "Fancy Spice", "price": 8100.0, "stock_qty": 50},
}

ESTABLISHED = {
    "name": "Ramesh Kirana",
    "credit_limit": 20000,
    "outstanding": 4500,
    "is_new": 0,
    "usual_basket": [{"item": "Sugar", "qty": 20}],
}


def test_rulebook_loads_with_expected_keys():
    rules = load_rules()
    assert rules["order"]["auto_confirm_max_amount"] == 15000
    assert rules["order"]["unusual_qty_multiplier"] == 3
    assert rules["delivery"]["fee"] == 40
    assert rules["delivery"]["free_above"] == 2000
    assert rules["packing"]["mismatch_tolerance"] == 0


def test_routine_order_auto_confirms():
    d = evaluate({"lines": [{"item_id": 1, "qty": 5}]}, ESTABLISHED, STOCK)
    assert d.action == AUTO_CONFIRM
    assert any("Routine" in r for r in d.reasons)


def test_credit_over_limit_asks_owner_with_rupee_sentence():
    # Outstanding 7,500 + order 24,300 = 31,800 vs limit 25,000.
    customer = dict(ESTABLISHED, name="Sharma Stores",
                    credit_limit=25000, outstanding=7500,
                    usual_basket=[{"item": "Fancy Spice", "qty": 3}])
    d = evaluate({"lines": [{"item_id": 3, "qty": 3}]}, customer, STOCK)
    assert d.action == ASK_OWNER
    assert (
        "Order of ₹24,300 would put Sharma Stores at ₹31,800"
        " against a ₹25,000 credit limit." in d.reasons
    )


def test_new_customer_asks_owner():
    d = evaluate({"lines": [{"item_id": 1, "qty": 1}]},
                 dict(ESTABLISHED, is_new=1), STOCK)
    assert d.action == ASK_OWNER
    assert any("new customer" in r for r in d.reasons)


def test_short_stock_rejects():
    d = evaluate({"lines": [{"item_id": 1, "qty": 600}]}, ESTABLISHED, STOCK)
    assert d.action == REJECT_SHORT_STOCK
    assert any("Only 500 Sugar in stock" in r for r in d.reasons)


def test_unusual_qty_asks_customer():
    # 100kg is 5x the usual 20kg basket.
    d = evaluate({"lines": [{"item_id": 1, "qty": 100}]}, ESTABLISHED, STOCK)
    assert d.action == ASK_CUSTOMER
    assert any("3x" in r and "usual 20" in r for r in d.reasons)


def test_big_ticket_asks_owner_despite_clean_credit():
    rich = dict(ESTABLISHED, credit_limit=100000, outstanding=0,
                usual_basket=[{"item": "Fancy Spice", "qty": 3}])
    d = evaluate({"lines": [{"item_id": 3, "qty": 3}]}, rich, STOCK)
    assert d.action == ASK_OWNER
    assert any("auto-confirm limit" in r for r in d.reasons)
