"""Morning briefing: SQL facts + short Hinglish summary (LLM or template).

FACTS are pure SQL over orders/events/customers/items — the LLM only turns
them into ≤8 lines of friendly Hinglish. prompts/briefing.txt (supplied by
the owner) holds the wording instructions; until it exists, summaries come
from the deterministic template below. A daily 8:00 asyncio loop pre-warms
the same briefing; the portal button always works on demand.
"""

from __future__ import annotations

import asyncio
import json
import statistics
from datetime import datetime, timedelta
from app import clock
from pathlib import Path

from app.ai import llm
from app.db import get_conn

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT / "prompts" / "briefing.txt"  # owner-supplied; may not exist yet
MAX_LINES = 8  # summary is short, whatever the model returns
RUNOUT_DAYS = 3  # items gone within this are flagged
OVER_LIMIT_PCT = 80  # khata customers at/above this get named
QUIET_MULT = 1.5  # silent for 1.5x the usual gap -> flagged
SCHED_HOUR = 8  # daily pre-warm at 08:00 local

CACHE: dict = {}  # {"date": str, "summary": str, "facts": dict}


def _parse(ts: str) -> datetime:
    """created_at ISO strings (always UTC in this DB)."""
    return datetime.fromisoformat(ts)


def _day_orders(conn, day) -> list:
    """Yesterday's real orders (seed demo rows count: they ARE the history)."""
    return [dict(r) for r in conn.execute("SELECT * FROM orders").fetchall()
            if _parse(r["created_at"]).date() == day
            and r["status"] not in ("CANCELLED", "REJECTED")]


def _facts_quiet(conn, now: datetime) -> list[dict]:
    """Established customers silent for 1.5x their usual order gap."""
    quiet = []
    for c in conn.execute(
            "SELECT * FROM customers WHERE is_new = 0 ORDER BY id"):
        stamps = sorted(
            _parse(r["created_at"]) for r in conn.execute(
                "SELECT created_at FROM orders WHERE customer_id = ?"
                " AND status NOT IN ('CANCELLED','REJECTED')", (c["id"],)))
        if len(stamps) < 2:
            continue
        gaps = [(b - a).total_seconds() / 86400
                for a, b in zip(stamps, stamps[1:])]
        usual = statistics.median(gaps)
        since = (now - stamps[-1]).total_seconds() / 86400
        if usual > 0 and since > QUIET_MULT * usual:
            quiet.append({"name": c["name"], "days_since": round(since, 1),
                          "usual_every_days": round(usual, 1)})
    return sorted(quiet, key=lambda q: -q["days_since"])


def compute_facts(db_path=None, now: datetime | None = None) -> dict:
    """Every briefing number, SQL only. Pure function of the DB + clock."""
    from app import owner as owner_mod  # local: keep ai/ import-light

    now = now or clock.now()
    yesterday = (now - timedelta(days=1)).date()
    conn = get_conn(db_path)
    try:
        orders = _day_orders(conn, yesterday)
        touched_ids = {r["order_id"] for r in conn.execute(
            "SELECT DISTINCT order_id FROM events WHERE actor = 'owner'")}
        touched = [o for o in orders if o["id"] in touched_ids]
        over = [{"name": c["name"], "outstanding": c["outstanding"],
                 "limit": c["credit_limit"],
                 "pct": round(100 * c["outstanding"] / c["credit_limit"])}
                for c in conn.execute("SELECT * FROM customers")
                if c["credit_limit"] > 0
                and c["outstanding"] >= OVER_LIMIT_PCT / 100 * c["credit_limit"]]
        runout = [{"name": it["name"], "stock": it["stock"],
                   "unit": it["unit"], "days_left": it["days_left"]}
                  for it in owner_mod.stock_report(db_path)
                  if it["days_left"] is not None and it["days_left"] <= RUNOUT_DAYS]
        problems = [
            {"order_id": r["order_id"], "kind": r["kind"],
             "message": r["message"][:160]}
            for r in conn.execute(
                "SELECT order_id, kind, message FROM events WHERE kind IN"
                " ('mismatch','problem','error') ORDER BY id DESC LIMIT 20")]
        return {
            "date": yesterday.isoformat(),
            "orders": len(orders),
            "revenue": round(sum(o["total"] for o in orders), 2),
            "auto_handled": len(orders) - len(touched),
            "owner_touched": len(touched),
            "pending_approval": sum(
                1 for r in conn.execute(
                    "SELECT 1 FROM orders WHERE status = 'AWAITING_APPROVAL'")),
            "pending_mismatch": sum(
                1 for r in conn.execute(
                    "SELECT 1 FROM orders WHERE status = 'PACK_MISMATCH'")),
            "over_limit": sorted(over, key=lambda c: -c["pct"]),
            "quiet": _facts_quiet(conn, now),
            "runout": runout,
            "exceptions": problems,
        }
    finally:
        conn.close()


def template_summary(facts: dict) -> str:
    """Deterministic Hinglish fallback (also used until briefing.txt exists)."""
    lines = [
        f"Kal {facts['orders']} order aaye (₹{facts['revenue']:,.0f}).",
        f"Munshi ne {facts['auto_handled']} of {facts['orders']} orders"
        " bina aapke sambhale.",
    ]
    if facts["pending_approval"] or facts["pending_mismatch"]:
        lines.append(f"Abhi {facts['pending_approval']} approval aur"
                     f" {facts['pending_mismatch']} mismatch aapka intezaar"
                     " kar rahe hain.")
    for c in facts["over_limit"][:3]:
        lines.append(f"{c['name']} udhaar {c['pct']}% par hai"
                     f" (₹{c['outstanding']:,.0f}).")
    for q in facts["quiet"][:2]:
        lines.append(f"{q['name']} {q['days_since']} din se order nahi aaya"
                     f" (usually har {q['usual_every_days']} din).")
    for it in facts["runout"][:3]:
        lines.append(f"{it['name']} lagbhag {it['days_left']} din me khatm"
                     f" ({it['stock']:g} {it['unit']} bacha).")
    for p in facts["exceptions"][:2]:
        lines.append(f"Order #{p['order_id']}: {p['message'][:80]}")
    return "\n".join(lines[:MAX_LINES])


def summarize(facts: dict) -> tuple[str, float, bool]:
    """Hinglish summary via briefing.txt + LLM, else the template.

    Returns (text, seconds, llm_used). Never raises: any model trouble
    falls back to the deterministic template.
    """
    import time

    t0 = time.perf_counter()
    if not PROMPT_PATH.is_file():
        return template_summary(facts), time.perf_counter() - t0, False
    try:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        facts_json = json.dumps(facts, ensure_ascii=False)
        prompt = prompt.replace("{facts}", facts_json) \
            if "{facts}" in prompt else prompt + "\n\nFacts (JSON):\n" + facts_json
        text, _secs = llm.chat([{"role": "user", "content": prompt}])
        kept = [ln.strip() for ln in text.splitlines() if ln.strip()][:MAX_LINES]
        if not kept:
            raise ValueError("empty summary")
        return "\n".join(kept), time.perf_counter() - t0, True
    except Exception:
        return template_summary(facts), time.perf_counter() - t0, False


def briefing(db_path=None, now: datetime | None = None) -> dict:
    """Facts + summary in one call (what the portal button fetches)."""
    facts = compute_facts(db_path, now)
    text, seconds, used = summarize(facts)
    return {"date": facts["date"], "summary": text, "facts": facts,
            "seconds": round(seconds, 2), "llm_used": used}


# -- daily 8:00 pre-warm (button works on demand regardless) ------------------------

def next_run_at(now: datetime) -> datetime:
    """Next 08:00 local after `now` (pure, tested)."""
    eight = now.replace(hour=SCHED_HOUR, minute=0, second=0, microsecond=0)
    return eight if now < eight else eight + timedelta(days=1)


async def scheduler_loop(db_path=None) -> None:
    """Sleep until 8:00, warm the cache, log an event, repeat forever."""
    from datetime import datetime as _dt  # local: keep module import-light

    while True:
        wait = (next_run_at(_dt.now().astimezone())
                - _dt.now().astimezone()).total_seconds()
        await asyncio.sleep(max(wait, 1))
        try:
            data = briefing(db_path)
            CACHE.update({"date": data["date"], "summary": data["summary"],
                          "facts": data["facts"]})
            conn = get_conn(db_path)
            try:
                conn.execute(
                    "INSERT INTO events (order_id, actor, kind, message,"
                    " data_json, ts) VALUES (NULL, 'system', 'briefing',"
                    " 'Morning briefing ready.', '{}', ?)",
                    (clock.now().isoformat(),))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass  # a missed morning must never kill the server loop


def start_scheduler(db_path=None) -> None:
    """Fire-and-forget the daily loop (call once, from app startup)."""
    asyncio.get_running_loop().create_task(scheduler_loop(db_path))
