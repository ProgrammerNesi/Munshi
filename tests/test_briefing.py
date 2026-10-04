"""Morning briefing tests: SQL facts, template/LLM summary, scheduler math."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai import briefing as briefing_mod  # noqa: E402
from app import engine  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.main import app  # noqa: E402
from app.rules import AUTO_CONFIRM, Decision  # noqa: E402
from scripts.seed import seed  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Seeded tmp DB (30 days of history — real briefing material)."""
    path = tmp_path / "brief.db"
    seed(path)
    monkeypatch.setenv("MUNSHI_DB", str(path))
    monkeypatch.delenv("MUNSHI_MOCK_AI", raising=False)
    return str(path)


def test_facts_shape_and_revenue_crosscheck(db):
    facts = briefing_mod.compute_facts(db)
    assert set(facts) >= {"date", "orders", "revenue", "auto_handled",
                          "owner_touched", "pending_approval",
                          "pending_mismatch", "over_limit", "quiet",
                          "runout", "exceptions"}
    assert facts["orders"] > 0  # seed writes ~daily orders per customer
    assert facts["auto_handled"] + facts["owner_touched"] == facts["orders"]
    conn = get_conn(db)
    try:
        rev = conn.execute(
            "SELECT ROUND(SUM(total), 2) AS s FROM orders"
            " WHERE substr(created_at, 1, 10) = ?"
            " AND status NOT IN ('CANCELLED','REJECTED')",
            (facts["date"],)).fetchone()["s"]
    finally:
        conn.close()
    assert facts["revenue"] == (rev or 0)


def test_over_limit_and_quiet_and_runout(db):
    conn = get_conn(db)
    try:
        conn.execute("UPDATE customers SET outstanding = 9000 WHERE id = 5")
        conn.execute("UPDATE customers SET is_new = 0 WHERE id = 10")
        now = datetime.now(timezone.utc)
        for days_ago in (30, 29):  # gap 1d, silent 29d -> flagged quiet
            ts = (now - timedelta(days=days_ago)).isoformat()
            conn.execute(
                "INSERT INTO orders (customer_id, status, transcript, total,"
                " created_at, updated_at) VALUES (10, 'delivered', 'old', 100,"
                " ?, ?)", (ts, ts))
        conn.execute("UPDATE items SET stock_qty = 5 WHERE id = 1")  # sugar
        conn.commit()
    finally:
        conn.close()
    facts = briefing_mod.compute_facts(db)
    gupta = next(c for c in facts["over_limit"] if c["name"] == "Gupta Store")
    assert gupta["pct"] == 180  # 9000 of 5000: over, not just near
    assert any(q["name"] == "Farhan Fresh Mart (New)" for q in facts["quiet"])
    sugar = next(i for i in facts["runout"] if i["name"] == "Sugar")
    assert sugar["days_left"] <= 3


def test_exceptions_catch_mismatch(db):
    oid = engine.create_order_from_draft(
        1, [{"item_id": 1, "qty": 2}], "do kilo cheeni", db_path=db)
    engine.apply_decision(oid, Decision(AUTO_CONFIRM, ["t"]), db_path=db)
    engine.assign_staff(oid, db_path=db)
    engine.submit_pack_counts(oid, {1: 1.0}, db_path=db)
    kinds = [p["kind"] for p in briefing_mod.compute_facts(db)["exceptions"]]
    assert "mismatch" in kinds


def test_template_summary_highlight(db):
    facts = briefing_mod.compute_facts(db)
    text = briefing_mod.template_summary(facts)
    assert f"Munshi ne {facts['auto_handled']} of {facts['orders']} orders" \
        in text
    assert len([ln for ln in text.splitlines() if ln.strip()]) <= 8


def test_llm_summary_truncated_to_8_lines(db, tmp_path, monkeypatch):
    prompt = tmp_path / "briefing.txt"
    prompt.write_text("Summarise these facts:\n{facts}", encoding="utf-8")
    monkeypatch.setattr(briefing_mod, "PROMPT_PATH", prompt)
    monkeypatch.setattr("app.ai.llm.chat",
                        lambda messages: ("\n".join(f"line {i}" for i in range(10)), 1.2))
    text, seconds, used = briefing_mod.summarize(
        briefing_mod.compute_facts(db))
    assert used is True and seconds >= 0
    assert text.splitlines() == [f"line {i}" for i in range(8)]


def test_llm_failure_falls_back_to_template(db, tmp_path, monkeypatch):
    prompt = tmp_path / "briefing.txt"
    prompt.write_text("Summarise:\n{facts}", encoding="utf-8")
    monkeypatch.setattr(briefing_mod, "PROMPT_PATH", prompt)

    def _boom(messages, tools=None):
        raise RuntimeError("ollama down")
    monkeypatch.setattr("app.ai.llm.chat", _boom)
    text, _s, used = briefing_mod.summarize(briefing_mod.compute_facts(db))
    assert used is False and "Munshi ne" in text


def test_missing_prompt_file_uses_template(db, tmp_path, monkeypatch):
    monkeypatch.setattr(briefing_mod, "PROMPT_PATH",
                        tmp_path / "no-such-file.txt")
    text, _s, used = briefing_mod.summarize(briefing_mod.compute_facts(db))
    assert used is False and "Munshi ne" in text


def test_next_run_math():
    tz = timezone.utc
    assert briefing_mod.next_run_at(
        datetime(2026, 1, 5, 7, 0, tzinfo=tz)) == datetime(2026, 1, 5, 8, tzinfo=tz)
    assert briefing_mod.next_run_at(
        datetime(2026, 1, 5, 9, 0, tzinfo=tz)) == datetime(2026, 1, 6, 8, tzinfo=tz)


def test_api_and_page(db):
    client = TestClient(app)
    body = client.get("/api/owner/briefing").json()
    assert set(body) >= {"date", "summary", "facts", "seconds", "llm_used"}
    assert "Munshi ne" in body["summary"]  # no prompt file on disk: template
    assert body["facts"]["orders"] > 0
    page = client.get("/owner/briefing")
    assert page.status_code == 200 and 'id="summary"' in page.text
    owner = client.get("/owner")
    assert owner.status_code == 200 and "/owner/briefing" in owner.text
