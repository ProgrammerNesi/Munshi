"""Agent loop tests with a scripted fake LLM (no models, no network)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import agent as agent_mod  # noqa: E402
from app import engine, messages, pipeline  # noqa: E402
from app.ai import llm  # noqa: E402
from app.db import get_conn  # noqa: E402
from app.rules import AUTO_CONFIRM, Decision  # noqa: E402
from scripts.seed import seed  # noqa: E402

OILS = ["Mustard Oil", "Refined Sunflower Oil", "Groundnut Oil"]


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Seeded tmp DB; agent resolves it per call (explicit path or env)."""
    path = tmp_path / "agent.db"
    seed(path)
    monkeypatch.setenv("MUNSHI_DB", str(path))
    monkeypatch.delenv("MUNSHI_MOCK_AI", raising=False)
    monkeypatch.delenv("MUNSHI_AGENT", raising=False)
    return str(path)


def _call(name, args):
    """One fake model message carrying a single tool call."""
    return {"content": "", "tool_calls": [{"name": name, "arguments": args}]}


def _fake_chat(script, monkeypatch):
    """Scripted chat_with_tools; exhausted scripts default to propose confirm."""
    def _fake(messages, tools):
        assert tools and len(tools) == 6, "agent must pass all six tools"
        if script:
            return script.pop(0), 0.01
        return _call("propose_action", {"kind": "confirm"}), 0.01
    monkeypatch.setattr("app.ai.llm.chat_with_tools", _fake)


def _order(db, customer_id=1, lines=None):
    lines = lines if lines is not None else [{"item_id": 1, "qty": 2}]
    return engine.create_order_from_draft(customer_id, lines, "test",
                                          db_path=db)


def _draft(raw, item_id, name, qty, unit, status="ok", candidates=None,
           score=100.0):
    return {"raw": raw, "item_id": item_id, "item_name": name, "qty": qty,
            "unit": unit, "score": score, "status": status,
            "candidates": candidates or []}


def _events(db, order_id, kind=None):
    conn = get_conn(db)
    try:
        q = "SELECT kind, message FROM events WHERE order_id = ?"
        args = [order_id]
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        return [dict(r) for r in conn.execute(q + " ORDER BY id", args)]
    finally:
        conn.close()


def test_clean_run_no_tools(db, monkeypatch):
    oid = _order(db)
    _fake_chat([_call("propose_action", {"kind": "confirm"})], monkeypatch)
    res = agent_mod.run_agent(oid, db_path=db)
    assert (res.kind, res.used_fallback) == ("confirm", False)
    assert len(res.tool_calls) == 1 and res.seconds >= 0
    assert _events(db, oid, "agent_proposal")


def test_resolve_ambiguous_then_confirm(db, monkeypatch):
    oid = _order(db)
    draft = [_draft("do kilo cheeni", 1, "Sugar", 2, "kg"),
             _draft("do litre tel", None, "tel", 2, "litre",
                    status="unresolved", candidates=OILS, score=90.0)]
    _fake_chat([
        _call("resolve_line", {"line_index": 1, "item_id": 12,
                               "why": "sarso tel, usual mustard oil"}),
        _call("evaluate_rules", {}),
        _call("propose_action", {"kind": "confirm"}),
    ], monkeypatch)
    res = agent_mod.run_agent(oid, draft_lines=draft, db_path=db)
    assert (res.kind, res.used_fallback) == ("confirm", False)
    assert res.tool_calls[0]["ok"] is True
    conn = get_conn(db)  # resolved line persisted for the bill
    try:
        items = sorted(r["item_id"] for r in conn.execute(
            "SELECT item_id FROM order_lines WHERE order_id = ?", (oid,)))
    finally:
        conn.close()
    assert items == [1, 12]
    assert len(_events(db, oid, "agent_tool")) >= 2


def test_resolve_rejected_below_threshold(db, monkeypatch):
    oid = _order(db)
    draft = [_draft("do kilo cheeni", 1, "Sugar", 2, "kg"),
             _draft("do litre tel", None, "tel", 2, "litre",
                    status="unresolved", candidates=OILS, score=90.0)]
    _fake_chat([
        _call("resolve_line", {"line_index": 1, "item_id": 27,
                               "why": "wild guess at soap"}),
    ], monkeypatch)
    res = agent_mod.run_agent(oid, draft_lines=draft, db_path=db)
    assert res.tool_calls[0]["ok"] is False  # tel vs soap: low score, not usual
    assert res.kind == "confirm"  # cheeni line alone is routine


def test_ask_customer_path(db, monkeypatch):
    oid = _order(db, lines=[{"item_id": 1, "qty": 100}])  # 5x the usual 20kg
    draft = [_draft("100 kilo cheeni", 1, "Sugar", 100, "kg")]
    text = "100 kilo cheeni? Usually 20 lete hain. HAAN likhiye."
    _fake_chat([_call("propose_action", {"kind": "ask_customer", "text": text})],
               monkeypatch)
    res = agent_mod.run_agent(oid, draft_lines=draft, db_path=db)
    assert (res.kind, res.text) == ("ask_customer", text)


def test_unknown_tool_falls_back(db, monkeypatch):
    oid = _order(db)
    _fake_chat([_call("teleport", {})], monkeypatch)
    res = agent_mod.run_agent(oid, db_path=db)
    assert res.used_fallback and "unknown tool" in res.fallback_reason
    assert res.kind == "confirm"  # deterministic rulebook: routine order
    assert _events(db, oid, "agent_fallback")


def test_repeated_call_falls_back(db, monkeypatch):
    oid = _order(db)
    same = _call("check_stock", {"item_id": 1, "qty": 2})
    _fake_chat([same, _call("check_stock", {"item_id": 1, "qty": 2})],
               monkeypatch)
    res = agent_mod.run_agent(oid, db_path=db)
    assert res.used_fallback and "repeated" in res.fallback_reason


def test_tool_disallowed_in_state(db, monkeypatch):
    oid = _order(db)
    engine.apply_decision(oid, Decision(AUTO_CONFIRM, []), db_path=db)
    _fake_chat([_call("check_stock", {"item_id": 1, "qty": 2})], monkeypatch)
    res = agent_mod.run_agent(oid, db_path=db)
    assert res.tool_calls[0]["ok"] is False
    assert "disabled" in res.tool_calls[0]["result"]
    assert res.used_fallback  # loop can only end in fallback from here


def test_less_cautious_proposal_overridden(db, monkeypatch):
    oid = _order(db, customer_id=4,  # Patel: 18k due, 50k limit
                 lines=[{"item_id": 3, "qty": 300}, {"item_id": 1, "qty": 100}])
    draft = [_draft("300 basmati", 3, "Basmati Rice", 300, "kg"),
             _draft("100 cheeni", 1, "Sugar", 100, "kg")]
    _fake_chat([_call("propose_action", {"kind": "confirm"})], monkeypatch)
    res = agent_mod.run_agent(oid, draft_lines=draft, db_path=db)
    assert res.kind == "ask_owner"  # rulebook (credit) overrules confirm
    assert _events(db, oid, "agent_overridden")


def test_invented_number_falls_back_to_template(db, monkeypatch):
    oid = _order(db)
    draft = [_draft("do litre tel", None, "tel", 2, "litre",
                    status="unresolved", candidates=OILS, score=90.0)]
    _fake_chat([_call("propose_action",
                      {"kind": "ask_customer", "text": "Pay Rs 999 advance?"})],
               monkeypatch)
    res = agent_mod.run_agent(oid, draft_lines=draft, db_path=db)
    assert res.kind == "ask_customer" and "999" not in res.text
    assert res.text == messages.clarify_item("do litre tel", OILS)


def test_agent_off_keeps_deterministic_path(db, monkeypatch):
    monkeypatch.setenv("MUNSHI_AGENT", "0")
    oid = _order(db)
    conn = get_conn(db)
    try:
        customer, stock = pipeline._context(conn, 1)[:2]
    finally:
        conn.close()
    d = pipeline.decide(oid, [{"item_id": 1, "qty": 2}], customer, stock,
                        db_path=db)
    assert d.action == AUTO_CONFIRM
    assert _events(db, oid, "agent_tool") == []
    assert _events(db, oid, "agent_fallback") == []


def test_agent_on_wiring_and_ask_text(db, monkeypatch):
    monkeypatch.setenv("MUNSHI_AGENT", "1")
    oid = _order(db, lines=[{"item_id": 1, "qty": 100}])
    draft = [_draft("100 kilo cheeni", 1, "Sugar", 100, "kg")]
    text = "100 kilo cheeni confirm? HAAN likhiye."
    _fake_chat([_call("propose_action", {"kind": "ask_customer", "text": text})],
               monkeypatch)
    conn = get_conn(db)
    try:
        customer, stock = pipeline._context(conn, 1)[:2]
    finally:
        conn.close()
    d = pipeline.decide(oid, [{"item_id": 1, "qty": 100}], customer, stock,
                        db_path=db, draft_lines=draft)
    assert d.action == "ASK_CUSTOMER" and d.ask_text == text
