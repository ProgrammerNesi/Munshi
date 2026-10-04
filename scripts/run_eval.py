"""Eval harness: STT -> extract -> match per clip, then score vs ground truth.

Reads eval/expected.json ([{file, customer_id, lines:[{item_id, qty}]}]),
runs each clip through the real pipeline on a scratch DB copy, and writes
eval/report.md: transcription shown, % fully correct, line-item accuracy,
qty accuracy, seconds per stage, failure table, safety-net counter, agent
metrics, a cross-config comparison table, and an `ollama ps` memory note.

Usage:
    python scripts/run_eval.py [--stt-backend mlx_whisper] [--llm-model gemma4:e4b]
                               [--agent on|off] [--db data/munshi.db]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import clock  # noqa: E402

EVAL = ROOT / "eval"
AUDIO = EVAL / "audio"
EXPECTED = EVAL / "expected.json"
REPORT = EVAL / "report.md"
COMPARISONS = EVAL / "comparisons.json"

# Order never saw a bill in these states: the safety net held.
UNBILLED = {"NEW", "CLARIFYING", "AWAITING_APPROVAL", "NEEDS_OWNER_APPROVAL",
            "NEEDS_CUSTOMER_CONFIRM", "REJECTED", "CANCELLED"}

# Transcript unit words -> canonical unit (for the unit-confusion check).
UNIT_WORDS = {"kilo": "kg", "kg": "kg", "gram": "g", "g": "g",
              "litre": "litre", "liter": "litre", "litr": "litre",
              "bori": "bori", "packet": "pack", "pack": "pack", "pkt": "pack",
              "dozen": "dozen", "peti": "box", "box": "box", "dabba": "box",
              "piece": "piece", "peace": "piece"}


def load_expected() -> list[dict]:
    """Ground truth list from eval/expected.json."""
    return json.loads(EXPECTED.read_text())


def _events(conn, order_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, actor, message, data_json FROM events WHERE order_id = ?"
        " ORDER BY id", (order_id,)).fetchall()
    out = []
    for kind, actor, message, data_json in rows:
        try:
            data = json.loads(data_json or "{}")
        except ValueError:
            data = {}
        out.append({"kind": kind, "actor": actor, "message": message,
                    "data": data, "seconds": data.get("seconds")})
    return out


def _stage_seconds(events: list[dict]) -> dict[str, float]:
    """Per-stage seconds from pipeline event rows (heard=STT, etc.)."""
    total: dict[str, float] = {}
    for e in events:
        if isinstance(e["seconds"], (int, float)):
            total[e["kind"]] = total.get(e["kind"], 0.0) + e["seconds"]
    return total


def run_clip(db_path, clip: dict) -> dict:
    """One clip through the real pipeline; returns everything scored later."""
    from app.db import get_conn
    from app.pipeline import process_message_now

    conn = get_conn(db_path)
    try:
        before = conn.execute("SELECT COALESCE(MAX(id),0) FROM orders").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO messages (customer_id, order_id, direction, text,"
            " audio_path, ts) VALUES (?,?, 'in', '', ?, datetime('now'))",
            (clip["customer_id"], None,
             str((AUDIO / clip["file"]).resolve())),
        )
        mid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    t0 = time.perf_counter()
    process_message_now(mid, db_path)
    wall = time.perf_counter() - t0
    conn = get_conn(db_path)
    try:
        order = conn.execute(
            "SELECT * FROM orders WHERE id > ? ORDER BY id DESC LIMIT 1",
            (before,)).fetchone()
        if order is None:
            return {"file": clip["file"], "customer_id": clip["customer_id"],
                    "expected": clip["lines"], "predicted": [], "order_id": None,
                    "status": "NONE", "decision": "NONE", "transcript": "",
                    "needs_retype": False, "stages": {}, "wall": wall, "events": []}
        oid = order["id"]
        predicted = [{"item_id": r[0], "qty": r[1]} for r in conn.execute(
            "SELECT item_id, qty FROM order_lines WHERE order_id = ?", (oid,))]
        events = _events(conn, oid)
        decision = next((e["data"].get("action", "NONE") for e in events
                         if e["kind"] == "decision"), "NONE")
        needs_retype = any(e["data"].get("needs_retype") for e in events)
        heard = next((e["message"] for e in events if e["kind"] == "heard"), "")
        transcript = heard[7:] if heard.startswith("Heard: ") else ""
        return {"file": clip["file"], "customer_id": clip["customer_id"],
                "expected": clip["lines"], "predicted": predicted,
                "order_id": oid, "status": order["status"], "decision": decision,
                "transcript": transcript, "needs_retype": needs_retype,
                "stages": _stage_seconds(events), "wall": wall, "events": events}
    finally:
        conn.close()


def _item_names(db_path) -> dict[int, tuple[str, str]]:
    from app.db import get_conn
    conn = get_conn(db_path)
    try:
        return {r[0]: (r[1], r[2]) for r in
                conn.execute("SELECT id, name, unit FROM items")}
    finally:
        conn.close()


def classify(clip_result: dict, names: dict[int, tuple[str, str]]) -> dict:
    """Score one clip: line/qty accuracy + a failure reason per expected line."""
    exp, pred = clip_result["expected"], clip_result["predicted"]
    pred_by_item = {}
    for p in pred:
        pred_by_item.setdefault(p["item_id"], []).append(p["qty"])
    line_ok = qty_ok = 0
    failures = []
    for e in exp:
        got = pred_by_item.get(e["item_id"], [])
        if not got:
            failures.append({"line": e, "reason": "wrong item"})
        elif any(abs(q - e["qty"]) < 0.005 for q in got):
            line_ok += 1
            qty_ok += 1
        else:
            line_ok += 1
            failures.append({"line": e,
                             "reason": f"wrong qty (expected {e['qty']},"
                                       f" got {got[0]})"})
    # Unit confusion: right item, but the transcript asked in another unit.
    words = set(clip_result["transcript"].lower().replace(",", " ").split())
    said = {UNIT_WORDS[w] for w in words if w in UNIT_WORDS}
    for p in pred:
        if p["item_id"] in names and said and \
                names[p["item_id"]][1] not in said:
            failures.append({"line": p, "reason": "unit confusion"})
    extra = [p for p in pred if p["item_id"] not in {e["item_id"] for e in exp}]
    for p in extra:
        failures.append({"line": p, "reason": "extra line"})
    fully = not failures and len(pred) == len(exp)
    if not clip_result["transcript"]:
        failures.append({"line": None, "reason": "STT error (empty transcript)"})
    elif clip_result["needs_retype"] or (not pred and exp):
        failures.append({"line": None,
                         "reason": "STT/extract error (needs_retype)"})
    return {"fully_correct": fully, "line_ok": line_ok, "qty_ok": qty_ok,
            "n_expected": len(exp), "failures": failures}


def score_all(results: list[dict], db_path) -> dict:
    """Aggregate metrics + safety-net counter + agent metrics over clips."""
    names = _item_names(db_path)
    scored = [classify(r, names) for r in results]
    n_exp = sum(s["n_expected"] for s in scored)
    full = sum(1 for r, s in zip(results, scored) if s["fully_correct"])
    safety_caught, overridden, agent_runs = 0, 0, 0
    tool_calls, agent_events = 0, 0
    for r, s in zip(results, scored):
        evts = r["events"]
        over = sum(1 for e in evts if e["kind"] == "agent_overridden")
        overridden += over
        tools = sum(1 for e in evts if e["kind"] == "agent_tool")
        n_agent = sum(1 for e in evts if e["kind"].startswith("agent_"))
        ran = n_agent > 0
        agent_runs += ran
        tool_calls += tools
        agent_events += n_agent
        # Caught = wrong extraction that never became a bill (unusual-qty
        # hold, clarifying question, approval hold or rejection). Overridden
        # proposals count inside this total when they prevented the bill.
        if not s["fully_correct"] and r["status"] in UNBILLED:
            safety_caught += 1
    n = len(results)
    return {
        "n_orders": n,
        "pct_full": round(100 * full / n, 1) if n else 0.0,
        "line_acc": round(100 * sum(s["line_ok"] for s in scored) / n_exp, 1)
        if n_exp else 0.0,
        "qty_acc": round(100 * sum(s["qty_ok"] for s in scored) / n_exp, 1)
        if n_exp else 0.0,
        "avg_sec": round(sum(r["wall"] for r in results) / n, 1) if n else 0.0,
        "avg_stage_sec": _avg_stages(results),
        "safety_net": safety_caught + overridden,
        "safety_caught_unbilled": safety_caught,
        "agent_overridden": overridden,
        "agent_runs": agent_runs,
        "agent_no_fallback_pct": round(100 * sum(
            1 for r in results
            if any(e["kind"].startswith("agent_") for e in r["events"])
            and not any(e["kind"] == "agent_fallback" for e in r["events"]))
            / agent_runs, 1) if agent_runs else 0.0,
        "agent_avg_tools": round(tool_calls / agent_runs, 1) if agent_runs else 0.0,
        "agent_avg_steps": round(agent_events / agent_runs, 1) if agent_runs else 0.0,
        "scored": scored,
    }


def _avg_stages(results: list[dict]) -> dict[str, float]:
    """Mean seconds per pipeline stage across clips."""
    sums: dict[str, float] = {}
    for r in results:
        for k, v in r["stages"].items():
            sums[k] = sums.get(k, 0.0) + v
    n = len(results) or 1
    return {k: round(v / n, 2) for k, v in sorted(sums.items())}


def update_comparisons(row: dict) -> list[dict]:
    """Upsert one config row into eval/comparisons.json; returns all rows."""
    rows = json.loads(COMPARISONS.read_text()) if COMPARISONS.is_file() else []
    key = (row["stt_backend"], row["llm_model"], row["agent"])
    rows = [r for r in rows
            if (r["stt_backend"], r["llm_model"], r["agent"]) != key]
    rows.append(row)
    COMPARISONS.write_text(json.dumps(rows, indent=2))
    return rows


def ollama_ps() -> str:
    """Memory note: which models are loaded right now (never fatal)."""
    try:
        out = subprocess.run(["ollama", "ps"], capture_output=True, text=True,
                             timeout=15)
        return out.stdout.strip() or "(ollama ps: no output)"
    except Exception as e:
        return f"(ollama ps unavailable: {e})"


def render_report(results: list[dict], metrics: dict, cfg: dict,
                  comparisons: list[dict], mem: str) -> str:
    """Full eval/report.md: metrics, transcripts, failures, safety, agent."""
    names = _item_names(cfg["db"])
    L = [f"# Munshi eval report",
         f"_Config: STT `{cfg['stt_backend']}`, LLM `{cfg['llm_model']}`,"
         f" agent `{cfg['agent']}` · {cfg['ts']} · {metrics['n_orders']} clips_",
         "",
         f"- Fully correct orders: **{metrics['pct_full']}%**",
         f"- Line-item accuracy: **{metrics['line_acc']}%**",
         f"- Qty accuracy: **{metrics['qty_acc']}%**",
         f"- Average seconds per order: **{metrics['avg_sec']}s**",
         "  (" + ", ".join(f"{k} {v}s"
                           for k, v in metrics["avg_stage_sec"].items()) + ")",
         "",
         "## Transcripts",
         ""]
    for r in results:
        L.append(f"- `{r['file']}` (customer {r['customer_id']}):"
                 f" {r['transcript'] or '(empty)'}")
    L += ["", "## Failures", ""]
    any_fail = False
    for r, s in zip(results, metrics["scored"]):
        for f in s["failures"]:
            any_fail = True
            want = f["line"]
            L.append(f"- `{r['file']}` [{r['decision']}/{r['status']}]:"
                     f" {f['reason']}"
                     + (f" (expected item {want['item_id']}"
                        f" qty {want.get('qty', '?')})" if want else ""))
    if not any_fail:
        L.append("- none: every clip fully correct")
    L += ["",
          "## Safety net",
          "",
          f"Caught by safety net: **{metrics['safety_net']}**"
          f" (unbilled wrong orders: {metrics['safety_caught_unbilled']},"
          f" agent_overridden events: {metrics['agent_overridden']}).",
          "A wrong extraction counts as caught when no bill went out for it"
          " (unusual-qty hold, clarifying question, approval hold or rejection).",
          "",
          "## Agent",
          "",
          f"- Orders with agent runs: {metrics['agent_runs']}",
          f"- Without fallback: **{metrics['agent_no_fallback_pct']}%**",
          f"- Average tool calls: **{metrics['agent_avg_tools']}**",
          f"- Average steps: **{metrics['agent_avg_steps']}**"
          " (agent_* events per agent-run order)",
          f"- Overridden proposals: **{metrics['agent_overridden']}**",
          "",
          "## Comparison (STT backend x LLM model)",
          "",
          "| STT backend | LLM model | agent | fully correct | line acc |"
          " qty acc | avg s/order | safety net |",
          "|---|---|---|---|---|---|---|---|"]
    for c in comparisons:
        L.append(f"| {c['stt_backend']} | {c['llm_model']} | {c['agent']} |"
                 f" {c['pct_full']}% | {c['line_acc']}% | {c['qty_acc']}% |"
                 f" {c['avg_sec']}s | {c['safety_net']} |")
    L += ["", "## Memory note (`ollama ps` at run time)", "", "```", mem,
          "```", ""]
    _ = names
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the Munshi order eval.")
    ap.add_argument("--stt-backend", default=None,
                    help="mlx_whisper|gemma_audio|mock (default: env/current)")
    ap.add_argument("--llm-model", default=None,
                    help="Ollama tag (default: env/current)")
    ap.add_argument("--agent", default="on", choices=["on", "off"],
                    help="agent loop on/off (default on)")
    ap.add_argument("--db", default=None, help="sqlite path (default data/munshi.db)")
    args = ap.parse_args()
    if args.stt_backend:
        os.environ["MUNSHI_STT_BACKEND"] = args.stt_backend
    if args.llm_model:
        os.environ["MUNSHI_LLM_MODEL"] = args.llm_model
    os.environ["MUNSHI_AGENT"] = "1" if args.agent == "on" else "0"

    from app.db import resolve_db_path
    src = resolve_db_path(args.db)
    tmp = tempfile.mktemp(prefix="munshi_eval_", suffix=".db")
    shutil.copy(str(src), tmp)
    try:
        clips = load_expected()
        print(f"eval: {len(clips)} clips on scratch DB (real DB untouched)")
        results = []
        for clip in clips:
            print(f"  ... {clip['file']}", flush=True)
            results.append(run_clip(tmp, clip))
        metrics = score_all(results, tmp)
        cfg = {"stt_backend": os.environ.get("MUNSHI_STT_BACKEND", "mlx_whisper"),
               "llm_model": os.environ.get("MUNSHI_LLM_MODEL", "gemma4:e4b"),
               "agent": args.agent, "db": tmp,
               "ts": clock.now().isoformat(timespec="seconds")}
        row = {"stt_backend": cfg["stt_backend"], "llm_model": cfg["llm_model"],
               "agent": cfg["agent"], "n_orders": metrics["n_orders"],
               "pct_full": metrics["pct_full"], "line_acc": metrics["line_acc"],
               "qty_acc": metrics["qty_acc"], "avg_sec": metrics["avg_sec"],
               "safety_net": metrics["safety_net"], "ts": cfg["ts"]}
        comparisons = update_comparisons(row)
        REPORT.write_text(render_report(results, metrics, cfg, comparisons,
                                        ollama_ps()))
        print(f"fully correct: {metrics['pct_full']}%"
              f" | line {metrics['line_acc']}% | qty {metrics['qty_acc']}%"
              f" | {metrics['avg_sec']}s/order | safety net {metrics['safety_net']}")
        print(f"report: {REPORT}")
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                Path(tmp + suffix).unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()
