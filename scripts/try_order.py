"""Try one order end to end: audio/text -> transcript -> draft -> rulebook.

Usage:
    python scripts/try_order.py "do kilo cheeni, paanch kilo atta" --customer 1
    python scripts/try_order.py voice_note.webm --customer 2 --stt-backend gemma_audio

Prints the transcript, STT backend + seconds, extraction JSON, each line's
match/status, the Phase-2 rulebook Decision, and seconds per stage.
Nothing is written to the DB (read-only: catalog + customer lookup).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai import extract as ai_extract  # noqa: E402
from app.ai import stt  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.rules import evaluate  # noqa: E402


def _load_context(db_path, customer_id):
    """Customer + stock dicts in the shape rules.evaluate expects."""
    conn = get_conn(db_path)
    try:
        c = conn.execute(
            "SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if c is None:
            sys.exit(f"No customer #{customer_id} (try 1-10 after seed).")
        basket = json.loads(c["usual_basket_json"])
        customer = {
            "name": c["name"], "credit_limit": c["credit_limit"],
            "outstanding": c["outstanding"], "is_new": c["is_new"],
            "usual_basket": basket,
        }
        stock = {r["id"]: {"name": r["name"], "price": r["price"],
                           "stock_qty": r["stock_qty"]}
                 for r in conn.execute("SELECT * FROM items")}
        return customer, stock, [entry["item"] for entry in basket]
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Try one order through STT+extract+rules.")
    ap.add_argument("input", help="audio file path OR raw transcript text")
    ap.add_argument("--customer", type=int, default=1, help="customer id (default 1)")
    ap.add_argument("--stt-backend", default=None,
                    help="mlx_whisper|gemma_audio|mock (overrides env)")
    ap.add_argument("--db", default=None, help="sqlite path (default data/munshi.db)")
    args = ap.parse_args()
    if args.stt_backend:
        os.environ["MUNSHI_STT_BACKEND"] = args.stt_backend

    customer, stock, usual_names = _load_context(args.db, args.customer)

    # Stage 1: transcript (audio -> STT, text -> as-is).
    t0 = time.perf_counter()
    if Path(args.input).is_file():
        res = stt.transcribe(args.input)
        transcript, stt_label = res.text, f"{res.backend} ({res.seconds:.1f}s)"
    else:
        transcript, stt_label = args.input, "text input (0.0s)"
    stt_seconds = time.perf_counter() - t0
    print(f"transcript: {transcript}")
    print(f"stt: {stt_label}")

    # Stage 2: extraction.
    t0 = time.perf_counter()
    draft = ai_extract.extract_order(transcript, usual_names=usual_names,
                                     db_path=args.db)
    extract_seconds = time.perf_counter() - t0
    print(f"extraction ({extract_seconds:.1f}s):")
    print(json.dumps(draft.to_dict(), ensure_ascii=False, indent=2))
    for ln in draft.lines:
        extra = f" candidates={ln.candidates}" if ln.candidates else ""
        print(f"  line: {ln.raw!r} -> {ln.item_name} x{ln.qty}"
              f" [{ln.status}, score={ln.score:.0f}]{extra}")

    # Stage 3: rulebook (only cleanly resolved lines can be judged).
    t0 = time.perf_counter()
    rule_lines = [{"item_id": ln.item_id, "qty": ln.qty} for ln in draft.lines
                  if ln.item_id is not None and ln.qty is not None]
    if draft.needs_retype:
        print("rulebook: skipped (needs_retype — ask the customer to resend)")
    elif not rule_lines:
        print("rulebook: skipped (no resolvable lines with quantities)")
    else:
        decision = evaluate({"lines": rule_lines}, customer, stock)
        print(f"rulebook ({time.perf_counter() - t0:.1f}s): {decision.action}")
        for reason in decision.reasons:
            print(f"  - {reason}")
    print(f"stage seconds: stt={stt_seconds:.1f}"
          f" extract={extract_seconds:.1f}")


if __name__ == "__main__":
    main()
