"""Prime the demo DB so three scenes happen live (idempotent, re-runnable).

Scene A: repeat customer Ramesh (id 1) sends a normal order -> auto-confirms.
Scene B: Gupta (id 5) is tuned near his credit limit, then orders past it ->
  owner approval (AWAITING_APPROVAL).
Scene C: Khan's (id 3) order confirms, then packing one line short triggers
  the mismatch lock (PACK_MISMATCH).

Usage:
    python scripts/demo_setup.py [--db data/munshi.db]
    MUNSHI_MOCK_AI=1 python scripts/demo_setup.py   # deterministic canned AI
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import engine  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.pipeline import process_message_now  # noqa: E402
from scripts.reset_demo import reset_demo  # noqa: E402

# (customer_id, transcript) per scene.
SCENE_A = (1, "do kilo cheeni, paanch kilo atta bhej do")
SCENE_B = (5, "50 kilo cheeni bhej do")
SCENE_C = (3, "2 kilo haldi bhej do")


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


def scene_a(db_path=None) -> int:
    """Repeat customer's normal order; returns order id (expect CONFIRMED)."""
    mid = _send_text(db_path, *SCENE_A)
    return process_message_now(mid, db_path)


def scene_b(db_path=None) -> int:
    """Tune Gupta near his limit, then push him past it (expect ASK_OWNER)."""
    conn = get_conn(db_path)
    try:
        price = conn.execute(
            "SELECT price FROM items WHERE id = 1").fetchone()[0]
        limit = conn.execute(
            "SELECT credit_limit FROM customers WHERE id = 5").fetchone()[0]
        # Leave headroom smaller than the order total: breach is certain.
        conn.execute("UPDATE customers SET outstanding = ? WHERE id = 5",
                     (limit - price + 1,))
        conn.commit()
    finally:
        conn.close()
    mid = _send_text(db_path, *SCENE_B)
    return process_message_now(mid, db_path)


def scene_c(db_path=None) -> int:
    """Confirm Khan's order, then pack one line short (expect PACK_MISMATCH)."""
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Prime the three demo scenes.")
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
    print("demo scenes:")
    ok = True
    for name, oid, got, want in results:
        mark = "PASS" if got == want else "FAIL"
        ok = ok and got == want
        print(f"  [{mark}] Scene {name}: order #{oid} status={got} (want {want})")
    if not ok:
        sys.exit("demo_setup: a scene missed — check transcripts/LLM output above")
    print("All three scenes live: approve Scene B in /owner,"
          " resolve Scene C there too.")


if __name__ == "__main__":
    main()
