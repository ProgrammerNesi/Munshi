"""Prime the demo DB so four scenes happen live (idempotent, re-runnable).

All scenes use Ramesh Kirana (id 1), so the owner portal shows one demo store.
Scene A: a normal order auto-confirms.
Scene B: the same store's order goes past its credit limit and waits for owner
  approval (AWAITING_APPROVAL).
Scene C: an order confirms, then packing one line short triggers the mismatch
  lock (PACK_MISMATCH).
Scene D: a labelled PACKING order is created for the live delay demo.

Usage:
    python scripts/demo_setup.py [--db data/munshi.db]
    MUNSHI_MOCK_AI=1 python scripts/demo_setup.py   # deterministic canned AI
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import engine  # noqa: E402
from app.ai import llm  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.pipeline import process_message_now  # noqa: E402
from app.rules import AUTO_CONFIRM, Decision  # noqa: E402
from scripts.reset_demo import reset_demo  # noqa: E402

# (customer_id, transcript) per scene. Keep demo activity on one customer store.
DEMO_CUSTOMER_ID = 1
SCENE_A = (DEMO_CUSTOMER_ID, "do kilo cheeni, paanch kilo atta bhej do")
SCENE_B = (DEMO_CUSTOMER_ID, "50 kilo cheeni bhej do")
SCENE_C = (DEMO_CUSTOMER_ID, "2 kilo haldi bhej do")


def _send_text(db_path, customer_id: int, text: str) -> int:
    """Store an inbound text message row; returns its id."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO messages (customer_id, order_id, direction, text,"
            " audio_path, ts) VALUES (?,?, 'in', ?, NULL,"
            " datetime('now'))",
            (customer_id, None, text),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _status(db_path, order_id: int) -> str:
    conn = get_conn(db_path)
    try:
        return conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()[0]
    finally:
        conn.close()


def _mock_lines(lines: list[tuple[str, str, float, str]]) -> None:
    """Give each scene a stable extraction when mock mode is enabled."""
    if os.environ.get("MUNSHI_MOCK_AI") == "1":
        llm.set_mock_json({
            "lines": [
                {"raw": raw, "item_guess": guess, "qty": qty, "unit": unit}
                for raw, guess, qty, unit in lines
            ],
            "notes": "",
        })


def scene_a(db_path=None) -> int:
    """Repeat customer's normal order; returns order id (expect CONFIRMED)."""
    _mock_lines([("2 kilo cheeni", "cheeni", 2, "kg"),
                 ("5 kilo atta", "atta", 5, "kg")])
    mid = _send_text(db_path, *SCENE_A)
    return process_message_now(mid, db_path)


def scene_b(db_path=None) -> int:
    """Temporarily tune the demo store's credit, then push past it."""
    _mock_lines([("50 kilo cheeni", "cheeni", 50, "kg")])
    conn = get_conn(db_path)
    try:
        price = conn.execute(
            "SELECT price FROM items WHERE id = 1").fetchone()[0]
        customer = conn.execute(
            "SELECT credit_limit, outstanding FROM customers WHERE id = ?",
            (DEMO_CUSTOMER_ID,),
        ).fetchone()
        limit, previous_outstanding = customer["credit_limit"], customer["outstanding"]
        # Leave headroom smaller than the order total: breach is certain.
        conn.execute("UPDATE customers SET outstanding = ? WHERE id = ?",
                     (limit - price + 1, DEMO_CUSTOMER_ID))
        conn.commit()
    finally:
        conn.close()
    mid = _send_text(db_path, *SCENE_B)
    try:
        return process_message_now(mid, db_path)
    finally:
        conn = get_conn(db_path)
        try:
            conn.execute(
                "UPDATE customers SET outstanding = ? WHERE id = ?",
                (previous_outstanding, DEMO_CUSTOMER_ID),
            )
            conn.commit()
        finally:
            conn.close()


def scene_c(db_path=None) -> int:
    """Confirm a demo-store order, then pack one line short."""
    _mock_lines([("2 kilo haldi", "haldi", 2, "kg")])
    mid = _send_text(db_path, *SCENE_C)
    oid = process_message_now(mid, db_path)
    if _status(db_path, oid) == "CONFIRMED":
        engine.assign_staff(oid, db_path=db_path)
    if _status(db_path, oid) != "PACKING":
        return oid  # extraction went sideways; caller reports it
    conn = get_conn(db_path)
    try:
        line = conn.execute(
            "SELECT item_id, qty FROM order_lines WHERE order_id = ?"
            " ORDER BY id LIMIT 1", (oid,)).fetchone()
    finally:
        conn.close()
    short = {line[0]: round(line[1] / 2, 2)} if line else {}
    if short:
        engine.submit_pack_counts(oid, short, db_path=db_path)
    return oid


def scene_d(db_path=None) -> int:
    """Create a clearly labelled PACKING order for the live delay demo."""
    oid = engine.create_order_from_draft(
        DEMO_CUSTOMER_ID, [{"item_id": 1, "qty": 2}],
        transcript="[DEMO SCENE D] two kilos of sugar; delay and tracking flow",
        db_path=db_path,
    )
    engine.apply_decision(
        oid, Decision(AUTO_CONFIRM, ["[DEMO SCENE D] controlled test order"]),
        db_path=db_path,
    )
    engine.assign_staff(oid, db_path=db_path)
    return oid


def main() -> None:
    ap = argparse.ArgumentParser(description="Prime the four demo scenes.")
    ap.add_argument("--db", default=None, help="sqlite path (default data/munshi.db)")
    args = ap.parse_args()
    reset_demo(args.db)
    results = []
    oid = scene_a(args.db)
    results.append(("A auto-confirm", oid, _status(args.db, oid), "PACKING"))
    oid = scene_b(args.db)
    results.append(("B credit approval", oid, _status(args.db, oid),
                    "AWAITING_APPROVAL"))
    oid = scene_c(args.db)
    results.append(("C mismatch lock", oid, _status(args.db, oid), "PACK_MISMATCH"))
    oid = scene_d(args.db)
    results.append(("D tracking delay", oid, _status(args.db, oid), "PACKING"))
    print("demo scenes:")
    ok = True
    for name, oid, got, want in results:
        mark = "PASS" if got == want else "FAIL"
        ok = ok and got == want
        print(f"  [{mark}] Scene {name}: order #{oid} status={got} (want {want})")
    if not ok:
        sys.exit("demo_setup: a scene missed — check transcripts/LLM output above")
    print("All four scenes live: approve Scene B, resolve Scene C, then use"
          " Scene D to demo delay → tracking → Reassign → delivery.")


if __name__ == "__main__":
    main()
