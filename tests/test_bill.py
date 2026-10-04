"""Unit tests: template lengths, bill math, LLM-intro number guard."""

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import bill as bill_mod  # noqa: E402
from app import engine, messages  # noqa: E402
from scripts.seed import seed  # noqa: E402


def test_every_template_fits_a_chat_bubble():
    """All messages.* templates stay under 160 chars, even with long inputs."""
    long_name = "Extra Long Premium Basmati Rice Deluxe Special Edition 5kg Pack"
    cases = [
        messages.listening(), messages.clarify_item("x", [long_name] * 3),
        messages.clarify_unit(long_name), messages.clarify_qty(long_name),
        messages.confirm_unusual_qty(long_name, 500, 2),
        messages.awaiting_owner(), messages.owner_declined(),
        messages.order_cancelled(), messages.short_stock(long_name, 12.5),
        messages.bill_intro(), messages.status_packing_started(),
        messages.status_out_for_delivery("~60 minutes"),
        messages.status_delivered("UPI"), messages.delivery_problem(),
        messages.could_not_understand(),
    ]
    assert cases and all(len(t) <= messages.MAX_LEN for t in cases)


def test_bill_math_and_render(tmp_path):
    db = tmp_path / "bill.db"
    seed(db)
    oid = engine.create_order_from_draft(
        1, [{"item_id": 1, "qty": 2}], "do kilo cheeni", db_path=db)
    b = bill_mod.build_bill(oid, db_path=db)
    assert b["lines"] == [
        {"name": "Sugar", "qty": 2, "unit": "kg", "rate": 45.0, "line_total": 90.0}
    ]
    assert (b["subtotal"], b["delivery_fee"], b["total"]) == (90.0, 40.0, 130.0)
    assert "within ~" in b["eta_text"]
    text = bill_mod.render_text(b)
    assert "Sugar" in text and "Total: ₹130" in text


def test_bill_intro_rejects_invented_numbers(tmp_path):
    db = tmp_path / "bill2.db"
    seed(db)
    oid = engine.create_order_from_draft(
        1, [{"item_id": 1, "qty": 2}], db_path=db)
    b = bill_mod.build_bill(oid, db_path=db)
    assert bill_mod.bill_intro(b, "Aapka bill ₹130 hai, shukriya!").startswith(
        "Aapka bill ₹130")
    assert bill_mod.bill_intro(b, "Aapka bill ₹100 hai, ₹30 discount!") == \
        messages.bill_intro()  # 100 never appears: template wins
    assert bill_mod.bill_intro(b) == messages.bill_intro()


def test_no_template_function_takes_an_llm():
    """messages.py stays LLM-free: only plain args, no imports beyond stdlib."""
    import app.messages as m

    assert not hasattr(m, "llm")
    for _name, fn in inspect.getmembers(m, inspect.isfunction):
        if _name.startswith("_"):
            continue
        for p in inspect.signature(fn).parameters.values():
            ann = p.annotation  # strings: messages.py uses `from __future__`
            ok = ann in (inspect.Parameter.empty, str, float, int, list,
                         "str", "float", "int", "list", "list[str]")
            ok = ok or getattr(ann, "__origin__", None) is list
            assert ok, (_name, ann)
