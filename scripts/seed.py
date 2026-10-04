"""Seed Munshi DB from CSVs + 30 days of FAKE demo order history.

DEMO DATA WARNING: customers/staff/phones are invented for local testing.
Historical orders are synthetic (seeded RNG) and labelled '[DEMO HISTORY]'
in transcript + a system/seed event row, so they are never mistaken for
real orders. Stock/outstanding stay at the CSV snapshot values — history
exists only so stock-usage stats (avg daily sale per item) can be computed.

Usage:
    python scripts/seed.py [--db data/munshi.db]
Idempotent: wipes all tables and re-seeds from scratch.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import secrets
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import clock  # noqa: E402
from app.db import DEMO_TABLES, get_conn, init_db, table_counts  # noqa: E402

FAKE_SEED = 42
HISTORY_DAYS = 30
ORDER_PROB_PER_DAY = 0.7  # ~21 orders per established customer

DATA = ROOT / "data"


def _read_csv(path: Path) -> list[dict]:
    """Read a CSV, skipping '#' comment lines (used for DEMO warnings)."""
    with open(path, newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def parse_basket(raw: str) -> list[dict]:
    """Parse 'Item Name:qty|Item Name:qty' into [{'item', 'qty'}]."""
    basket = []
    for part in (raw or "").split("|"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, qty = part.rsplit(":", 1)
        basket.append({"item": name.strip(), "qty": float(qty.strip())})
    return basket


def seed(
    db_path: str | Path | None = None,
    *,
    single_shop: bool = False,
) -> dict[str, int]:
    """Seed demo data; optionally limit the demo database to Ramesh Kirana."""
    init_db(db_path)
    conn = get_conn(db_path)
    try:
        # -- wipe everything for idempotency (masters + demo tables) --
        for t in DEMO_TABLES:
            conn.execute(f"DELETE FROM {t}")
        for t in ("items", "customers", "staff"):
            conn.execute(f"DELETE FROM {t}")

        # -- masters from CSV --
        items = _read_csv(DATA / "catalog.csv")
        for r in items:
            json.loads(r["aliases_json"])  # validate
            conn.execute(
                "INSERT INTO items (id, name, aliases_json, unit, price,"
                " stock_qty, reorder_level) VALUES (?,?,?,?,?,?,?)",
                (int(r["id"]), r["name"], r["aliases_json"], r["unit"],
                 float(r["price"]), float(r["stock_qty"]),
                 float(r["reorder_level"])),
            )
        price_of = {r["name"]: float(r["price"]) for r in items}
        item_id_of = {r["name"]: int(r["id"]) for r in items}

        customers = _read_csv(DATA / "customers.csv")
        if single_shop:
            customers = [row for row in customers if int(row["id"]) == 1]
        for r in customers:
            basket = parse_basket(r.get("usual_basket", ""))
            conn.execute(
                "INSERT INTO customers (id, name, phone, area, credit_limit,"
                " outstanding, is_new, usual_basket_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (int(r["id"]), r["name"], r["phone"], r["area"],
                 float(r["credit_limit"]), float(r["outstanding"]),
                 int(r["is_new"]), json.dumps(basket, ensure_ascii=False)),
            )

        for r in _read_csv(DATA / "staff.csv"):
            assert r["role"] in ("owner", "packer", "delivery"), r
            conn.execute(
                "INSERT INTO staff (id, name, role) VALUES (?,?,?)",
                (int(r["id"]), r["name"], r["role"]),
            )

        # -- 30 days of FAKE history for established customers only --
        # New customers (is_new=1) get no history: they are new by definition.
        rng = random.Random(FAKE_SEED)
        now = clock.now()
        packers = [2, 3]  # seeded staff ids with role packer
        deliverers = [4, 5]  # seeded staff ids with role delivery
        n_orders = 0
        for c in customers:
            if int(c["is_new"]):
                continue
            basket = parse_basket(c.get("usual_basket", ""))
            if not basket:
                continue
            cid = int(c["id"])
            for days_ago in range(HISTORY_DAYS, 0, -1):
                if rng.random() > ORDER_PROB_PER_DAY:
                    continue
                day = now - timedelta(days=days_ago)
                ts = day.replace(hour=rng.randint(9, 20),
                                 minute=rng.randint(0, 59),
                                 second=0, microsecond=0)
                iso = ts.isoformat()
                # Pick 2-4 basket lines with +/-30% qty jitter.
                lines = rng.sample(basket, k=min(len(basket), rng.randint(2, 4)))
                total = 0.0
                desc = []
                priced = []
                for ln in lines:
                    qty = round(ln["qty"] * rng.uniform(0.7, 1.3), 2)
                    qty = max(qty, 0.5)
                    price = price_of[ln["item"]]
                    total += qty * price
                    priced.append((ln["item"], qty, price))
                    desc.append(f"{ln['item']} {qty:g}")
                total = round(total, 2)
                transcript = "[DEMO HISTORY] fake order: " + ", ".join(desc)
                cur = conn.execute(
                    "INSERT INTO orders (customer_id, status, transcript,"
                    " total, payment_mode, packer_id, delivery_id,"
                    " track_token, stage_entered_at,"
                    " created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, "delivered", transcript, total,
                     rng.choice(["cash", "credit"]),
                     rng.choice(packers), rng.choice(deliverers),
                     secrets.token_urlsafe(16), iso, iso, iso),
                )
                oid = cur.lastrowid
                for name, qty, price in priced:
                    conn.execute(
                        "INSERT INTO order_lines (order_id, item_id, qty,"
                        " unit_price, packed_qty) VALUES (?,?,?,?,?)",
                        (oid, item_id_of[name], qty, price, qty),
                    )
                conn.execute(
                    "INSERT INTO events (order_id, actor, kind, message,"
                    " data_json, ts) VALUES (?,?,?,?,?,?)",
                    (oid, "system", "seed",
                     "[DEMO HISTORY] synthetic order for usage stats",
                     json.dumps({"fake": True, "demo_history": True}), iso),
                )
                conn.execute(
                    "INSERT INTO messages (customer_id, order_id, direction,"
                    " text, audio_path, ts) VALUES (?,?,?,?,?,?)",
                    (cid, oid, "in", transcript, None, iso),
                )
                n_orders += 1

        conn.commit()
        print(f"seeded {len(items)} items, {len(customers)} customers,"
              f" {n_orders} FAKE history orders (marked [DEMO HISTORY])")
        return table_counts(db_path)
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed Munshi demo DB.")
    ap.add_argument("--db", default=None, help="sqlite path (default data/munshi.db)")
    ap.add_argument(
        "--single-shop",
        action="store_true",
        help="seed only Ramesh Kirana for a focused single-shop demo",
    )
    args = ap.parse_args()
    counts = seed(args.db, single_shop=args.single_shop)
    for t, n in counts.items():
        print(f"  {t}: {n}")


if __name__ == "__main__":
    main()
