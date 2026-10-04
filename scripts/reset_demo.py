"""Reset demo/transactional data, keep masters.

Wipes orders / order_lines / events / messages / notifications, then
restores item stock_qty and customer outstanding balances to the seeded
CSV snapshot values (data/catalog.csv, data/customers.csv).

Usage:
    python scripts/reset_demo.py [--db data/munshi.db]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import DEMO_TABLES, get_conn, init_db, table_counts  # noqa: E402

DATA = ROOT / "data"


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def reset_demo(db_path: str | Path | None = None) -> dict[str, int]:
    """Wipe demo tables + restore stock/outstanding. Returns row counts."""
    init_db(db_path)  # no-op if schema already exists
    conn = get_conn(db_path)
    try:
        # FK-safe order: children first.
        for t in DEMO_TABLES:
            conn.execute(f"DELETE FROM {t}")
        # Restart AUTOINCREMENT ids for demo tables.
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN"
            " ('orders','order_lines','events','messages','notifications',"
            " 'attention_items')"
        )
        # Restore snapshots from CSVs.
        for r in _read_csv(DATA / "catalog.csv"):
            conn.execute(
                "UPDATE items SET stock_qty = ?, reorder_level = ? WHERE id = ?",
                (float(r["stock_qty"]), float(r["reorder_level"]), int(r["id"])),
            )
        for r in _read_csv(DATA / "customers.csv"):
            conn.execute(
                "UPDATE customers SET outstanding = ? WHERE id = ?",
                (float(r["outstanding"]), int(r["id"])),
            )
        conn.commit()
        counts = table_counts(db_path)
        return counts
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Reset Munshi demo data.")
    ap.add_argument("--db", default=None, help="sqlite path (default data/munshi.db)")
    args = ap.parse_args()
    counts = reset_demo(args.db)
    print("demo reset: orders/lines/events/messages/notifications wiped;")
    print("stock_qty + outstanding restored from CSV snapshots.")
    for t, n in counts.items():
        print(f"  {t}: {n}")


if __name__ == "__main__":
    main()
