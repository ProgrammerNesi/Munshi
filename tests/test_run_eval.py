"""Phase 8: eval scoring pure functions + offline end-to-end (mock backends)."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai import llm  # noqa: E402
from app.db import get_conn  # noqa: E402
from scripts import run_eval  # noqa: E402
from scripts.seed import seed  # noqa: E402

NAMES = {1: ("Sugar", "kg"), 2: ("Wheat Flour (Atta)", "kg")}


def _res(exp, pred, **kw):
    base = {"file": "t.wav", "customer_id": 1, "expected": exp,
            "predicted": pred, "order_id": 1, "status": "CONFIRMED",
            "decision": "AUTO_CONFIRM", "transcript": "do kilo cheeni",
            "needs_retype": False, "stages": {}, "wall": 1.0, "events": []}
    base.update(kw)
    return base


def test_classify_fully_correct():
    r = _res([{"item_id": 1, "qty": 2}], [{"item_id": 1, "qty": 2}])
    s = run_eval.classify(r, NAMES)
    assert s["fully_correct"] and not s["failures"]
    assert (s["line_ok"], s["qty_ok"], s["n_expected"]) == (1, 1, 1)


def test_classify_wrong_item_and_qty():
    r = _res([{"item_id": 1, "qty": 2}, {"item_id": 2, "qty": 5}],
             [{"item_id": 1, "qty": 3}])
    s = run_eval.classify(r, NAMES)
    assert not s["fully_correct"]
    assert (s["line_ok"], s["qty_ok"]) == (1, 0)
    reasons = [f["reason"] for f in s["failures"]]
    assert any(r.startswith("wrong qty") for r in reasons)
    assert any(r == "wrong item" for r in reasons)


def test_classify_empty_transcript_is_stt_error():
    r = _res([{"item_id": 1, "qty": 2}], [], transcript="")
    s = run_eval.classify(r, NAMES)
    assert any("STT error" in f["reason"] for f in s["failures"])


def test_safety_net_counts_unbilled_plus_overridden():
    ok = _res([{"item_id": 1, "qty": 2}], [{"item_id": 1, "qty": 2}])
    held = _res([{"item_id": 1, "qty": 2}], [{"item_id": 2, "qty": 2}],
                status="CLARIFYING", decision="ASK_CUSTOMER")
    bad = _res([{"item_id": 1, "qty": 2}], [{"item_id": 2, "qty": 2}],
               status="CONFIRMED", decision="AUTO_CONFIRM")
    bad["events"] = [{"kind": "agent_overridden", "actor": "agent",
                      "message": "m", "data": {}, "seconds": 0.1}]
    import sqlite3
    import app.db
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE items (id INTEGER, name TEXT, unit TEXT)")
    conn.execute("INSERT INTO items VALUES (1,'Sugar','kg'),(2,'Atta','kg')")
    real = app.db.get_conn
    app.db.get_conn = lambda db_path=None: conn
    try:
        m = run_eval.score_all([ok, held, bad], "ignored")
    finally:
        app.db.get_conn = real
        conn.close()
    assert m["pct_full"] == round(100 / 3, 1)
    assert m["safety_net"] == 2  # 1 unbilled-caught + 1 overridden event
    assert m["agent_overridden"] == 1 and m["safety_caught_unbilled"] == 1


def test_comparisons_upsert(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "COMPARISONS", tmp_path / "c.json")
    run_eval.update_comparisons({"stt_backend": "mock", "llm_model": "m",
                                 "agent": "off", "pct_full": 50})
    run_eval.update_comparisons({"stt_backend": "mock", "llm_model": "m",
                                 "agent": "off", "pct_full": 60})
    rows = json.loads((tmp_path / "c.json").read_text())
    assert len(rows) == 1 and rows[0]["pct_full"] == 60


def test_offline_end_to_end_mock(tmp_path, monkeypatch):
    db = tmp_path / "eval.db"
    seed(db)
    monkeypatch.setenv("MUNSHI_MOCK_AI", "1")
    monkeypatch.setenv("MUNSHI_STT_BACKEND", "mock")
    monkeypatch.setenv("MUNSHI_AGENT", "off")
    llm.set_mock_json({"lines": [{"raw": "x", "item_guess": "cheeni",
                                  "qty": 2, "unit": "kg"}], "notes": ""})
    clip = {"file": "order1.wav", "customer_id": 1,
            "lines": [{"item_id": 1, "qty": 2}]}
    (tmp_path / "order1.wav").write_bytes(b"fake-wav")
    monkeypatch.setattr(run_eval, "AUDIO", tmp_path)
    r = run_eval.run_clip(str(db), clip)
    assert r["transcript"] and r["predicted"] == [{"item_id": 1, "qty": 2.0}]
    assert r["status"] == "PACKING" and r["decision"] == "AUTO_CONFIRM"
    m = run_eval.score_all([r], str(db))
    assert m["pct_full"] == 100.0 and m["safety_net"] == 0
