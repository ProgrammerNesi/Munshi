"""Agent loop: investigate with read-only tools, propose ONE action.

The model gets a compact JSON context (customer, basket, draft, stock,
rulebook verdict) and may call tools, ending with exactly one
propose_action. Everything binding is enforced here in code, not in the
prompt: per-state tool gates, resolve_line validation, rulebook-caution
override, and ask-text checks. Any model-side failure falls back to the
deterministic rulebook decision. run_agent never raises to the caller.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

from app import messages
from app import rules as rules_mod
from app.ai import extract as ai_extract
from app.ai import llm
from app import clock
from app.db import get_conn
from app.rules import AUTO_CONFIRM, ASK_CUSTOMER, ASK_OWNER, REJECT_SHORT_STOCK

ROOT = Path(__file__).resolve().parent.parent

MAX_TURNS = 6  # model responses before we stop and fall back
RESOLVE_MIN_SCORE = 70  # resolve_line needs this match or a usual item
TOOL_STATES = {"NEW", "UNDERSTANDING", "CLARIFYING"}  # states where tools run

CAUTION = [AUTO_CONFIRM, ASK_CUSTOMER, ASK_OWNER, REJECT_SHORT_STOCK]
KIND_TO_ACTION = {"confirm": AUTO_CONFIRM, "ask_customer": ASK_CUSTOMER,
                  "ask_owner": ASK_OWNER,
                  "reject_short_stock": REJECT_SHORT_STOCK}
ACTION_TO_KIND = {v: k for k, v in KIND_TO_ACTION.items()}


@dataclass
class AgentResult:
    """One agent run: final kind, customer text, trace, and fallback flags."""

    kind: str  # confirm | ask_customer | ask_owner | reject_short_stock
    text: str  # customer-facing text (ask_customer only, else "")
    steps: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    seconds: float = 0.0
    used_fallback: bool = False
    fallback_reason: str = ""


# -- native Ollama tool schemas -------------------------------------------------

def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    """One Ollama {"type": "function", ...} tool definition."""
    return {"type": "function",
            "function": {"name": name, "description": desc,
                         "parameters": {"type": "object", "properties": props,
                                        "required": required}}}


TOOLS = [
    _tool("get_customer_history", "Past orders, outstanding and usual basket.",
          {"customer_id": {"type": "integer"}}, ["customer_id"]),
    _tool("search_catalog", "Fuzzy-search shop items by name or alias.",
          {"query": {"type": "string"}}, ["query"]),
    _tool("check_stock", "Shelf quantity and price for one item.",
          {"item_id": {"type": "integer"}, "qty": {"type": "number"}},
          ["item_id", "qty"]),
    _tool("resolve_line", "Pin an unclear draft line to a catalog item.",
          {"line_index": {"type": "integer"}, "item_id": {"type": "integer"},
           "why": {"type": "string"}},
          ["line_index", "item_id", "why"]),
    _tool("evaluate_rules", "Re-run the rulebook on the current draft.",
          {}, []),
    _tool("propose_action", "Finish: propose exactly one action.",
          {"kind": {"type": "string",
                    "enum": ["confirm", "ask_customer", "ask_owner",
                             "reject_short_stock"]},
           "text": {"type": "string"}}, ["kind"]),
]


# -- small helpers ---------------------------------------------------------------

def _now() -> str:
    """Demo-aware timestamp for ts / updated_at columns."""
    return clock.now().isoformat()


def _log(db_path, order_id, kind, message, seconds, data=None) -> None:
    """Best-effort reasoning-log row (logging must never break the agent)."""
    try:
        conn = get_conn(db_path)
        try:
            conn.execute(
                "INSERT INTO events (order_id, actor, kind, message, data_json, ts)"
                " VALUES (?,?,?,?,?,?)",
                (order_id, "agent", kind, message,
                 json.dumps({**(data or {}), "seconds": round(seconds, 2)}),
                 _now()))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _short(result: dict, limit: int = 300) -> str:
    """Shortened tool result for logs and the trace (owner portal friendly)."""
    return json.dumps(result, ensure_ascii=False, default=str)[:limit]


def _numbers(text: str) -> list[str]:
    """Digit runs, commas stripped: used to catch invented numbers."""
    return [n.replace(",", "") for n in re.findall(r"\d[\d,]*\.?\d*", text or "")]


# -- agent run state (one instance per run_agent call) -------------------------------

class _Run:
    """Mutable loop state: working draft, latest rulebook verdict, trace."""

    def __init__(self, order_id, status, customer, stock, catalog, usual_names,
                 draft, transcript, rules, db_path):
        self.order_id = order_id
        self.status = status
        self.customer = customer
        self.stock = stock  # {item_id: {name, price, stock_qty}}
        self.catalog = catalog  # matcher entries with unit + aliases
        self.by_id = {e["item_id"]: e for e in catalog}
        self.usual_names = usual_names
        self.draft = draft  # working copy: matched lines, edited by resolve
        self.transcript = transcript
        self.rules = rules
        self.db_path = db_path
        self.steps: list[str] = []
        self.tool_calls: list[dict] = []
        self.latest = self._baseline()
        self.numbers = self._context_numbers()

    # -- rulebook on the working draft --
    def _evaluable(self) -> list[dict]:
        """Draft lines the rulebook can judge (resolved item + quantity)."""
        return [{"item_id": ln["item_id"], "qty": ln["qty"]} for ln in self.draft
                if ln.get("item_id") is not None and ln.get("qty") is not None]

    def _baseline(self):
        """First verdict; empty drafts default to asking, never auto-confirm."""
        lines = self._evaluable()
        if not lines:
            return rules_mod.Decision(
                ASK_CUSTOMER, ["No resolvable lines yet — clarification needed."])
        return rules_mod.evaluate({"lines": lines}, self.customer, self.stock,
                                  self.rules)

    def _context_numbers(self) -> set:
        """Every number the model legitimately knows (for the invented check)."""
        strs, floats = set(), set()

        def add(value) -> None:
            for tok in _numbers(str(value)):
                strs.add(tok)
                try:
                    floats.add(float(tok))
                except ValueError:
                    pass

        add(self.transcript)
        for ln in self.draft:
            add(ln.get("qty"))
        for entry in self.customer.get("usual_basket", []):
            add(entry.get("qty"))
        for item_id in {ln.get("item_id") for ln in self.draft}:
            if item_id in self.stock:
                add(self.stock[item_id]["price"])
                add(self.stock[item_id]["stock_qty"])
        add(self.customer.get("outstanding"))
        add(self.customer.get("credit_limit"))
        return (strs, floats)

    def numbers_ok(self, text: str) -> bool:
        """Every number in the text appears in the order context."""
        strs, floats = self.numbers
        for tok in _numbers(text):
            try:
                if tok in strs or float(tok) in floats:
                    continue
            except ValueError:
                pass
            return False
        return True

    # -- tools (all read-only except resolve_line, which edits the draft) --
    def t_history(self, args: dict) -> dict:
        """Recent orders + khata for one customer."""
        cid = args.get("customer_id")
        conn = get_conn(self.db_path)
        try:
            row = conn.execute(
                "SELECT name, outstanding FROM customers WHERE id = ?",
                (cid,)).fetchone()
            if row is None:
                return {"ok": False, "error": f"No customer #{cid}."}
            recent = [
                {"id": r["id"], "status": r["status"], "total": r["total"],
                 "at": r["created_at"]} for r in conn.execute(
                    "SELECT id, status, total, created_at FROM orders"
                    " WHERE customer_id = ? ORDER BY id DESC LIMIT 5", (cid,))]
            return {"ok": True, "name": row["name"],
                    "outstanding": row["outstanding"],
                    "usual_basket": self.customer.get("usual_basket", []),
                    "recent": recent}
        finally:
            conn.close()

    def t_search(self, args: dict) -> dict:
        """Top-5 catalog hits for a name/alias query."""
        query = ai_extract.normalise(str(args.get("query", "")))
        if not query:
            return {"ok": False, "error": "Empty query."}
        scored = []
        for e in self.catalog:
            best = max([fuzz.WRatio(query, ai_extract.normalise(e["name"]))] +
                         [fuzz.WRatio(query, ai_extract.normalise(a))
                          for a in e["aliases"]])
            scored.append((best, e))
        scored.sort(key=lambda p: p[0], reverse=True)
        return {"ok": True, "hits": [
            {"item_id": e["item_id"], "name": e["name"], "unit": e["unit"],
             "price": next((s["price"] for i, s in self.stock.items()
                            if i == e["item_id"]), None),
             "score": round(best, 1)} for best, e in scored[:5]]}

    def t_check_stock(self, args: dict) -> dict:
        """Shelf quantity for one item and whether it covers qty."""
        try:
            item_id, qty = int(args["item_id"]), float(args["qty"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "Need numeric item_id and qty."}
        if item_id not in self.stock:
            return {"ok": False, "error": f"No item #{item_id}."}
        s = self.stock[item_id]
        return {"ok": True, "item_id": item_id, "name": s["name"],
                "unit": self.by_id[item_id]["unit"],
                "stock_qty": s["stock_qty"], "enough": s["stock_qty"] >= qty}

    def _resolve_score(self, line: dict, entry: dict) -> float:
        """Match score of the line's guess against one catalog entry."""
        base = line.get("item_name") if line.get("status") == "unresolved" \
            else line.get("raw", "")
        base = ai_extract.normalise(str(base))
        return max([fuzz.WRatio(base, ai_extract.normalise(entry["name"]))] +
                   [fuzz.WRatio(base, ai_extract.normalise(a))
                    for a in entry["aliases"]])

    def t_resolve(self, args: dict) -> dict:
        """Pin a draft line to an item after validating the match."""
        try:
            idx, item_id = int(args["line_index"]), int(args["item_id"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "Need numeric line_index and item_id."}
        if not (0 <= idx < len(self.draft)):
            return {"ok": False, "error": f"No draft line #{idx}."}
        entry = self.by_id.get(item_id)
        if entry is None:
            return {"ok": False, "error": f"No item #{item_id}."}
        line = self.draft[idx]
        unit = str(line.get("unit") or "unknown")
        if unit not in ("unknown", entry["unit"]):
            return {"ok": False,
                    "error": f"Unit {unit} does not fit {entry['name']}"
                             f" (sold per {entry['unit']})."}
        score = self._resolve_score(line, entry)
        if score < RESOLVE_MIN_SCORE and entry["name"] not in self.usual_names:
            return {"ok": False,
                    "error": f"Match score {score:.0f} below {RESOLVE_MIN_SCORE}"
                             f" and {entry['name']} is not a usual item."}
        line.update(item_id=item_id, item_name=entry["name"],
                    status="ok", score=round(score, 1))
        self._persist()
        self.latest = rules_mod.evaluate(
            {"lines": self._evaluable()}, self.customer, self.stock, self.rules)
        return {"ok": True, "line": line,
                "rulebook": {"action": self.latest.action,
                             "reasons": self.latest.reasons}}

    def _persist(self) -> None:
        """Mirror pipeline's line rewrite so the bill sees resolved lines."""
        lines = self._evaluable()
        if not lines:
            return
        subtotal = 0.0
        conn = get_conn(self.db_path)
        try:
            conn.execute("DELETE FROM order_lines WHERE order_id = ?",
                         (self.order_id,))
            for ln in lines:
                price = self.stock[ln["item_id"]]["price"]
                conn.execute(
                    "INSERT INTO order_lines (order_id, item_id, qty, unit_price,"
                    " packed_qty) VALUES (?,?,?,?,0)",
                    (self.order_id, ln["item_id"], ln["qty"], price))
                subtotal += price * ln["qty"]
            fee = 0.0 if subtotal >= float(self.rules["delivery"]["free_above"]) \
                else float(self.rules["delivery"]["fee"])
            conn.execute("UPDATE orders SET total = ? WHERE id = ?",
                         (round(subtotal + fee, 2), self.order_id))
            conn.commit()
        finally:
            conn.close()

    def t_evaluate(self, _args: dict) -> dict:
        """Re-run the rulebook on the current working draft."""
        self.latest = rules_mod.evaluate(
            {"lines": self._evaluable()}, self.customer, self.stock, self.rules) \
            if self._evaluable() else self._baseline()
        return {"ok": True, "action": self.latest.action,
                "reasons": self.latest.reasons,
                "lines_evaluated": len(self._evaluable())}


# -- entry point -------------------------------------------------------------------

def run_agent(order_id: int, draft_lines: list[dict] | None = None,
              db_path=None, rules: dict | None = None) -> AgentResult:
    """Investigate one order with tools, propose one action. Never raises."""
    t0 = time.perf_counter()
    rules = rules if rules is not None else rules_mod.load_rules()
    try:
        run = _load_run(order_id, draft_lines, db_path, rules)
    except Exception as e:
        return AgentResult(
            kind="ask_customer", text=messages.could_not_understand(),
            steps=["setup failed"], seconds=time.perf_counter() - t0,
            used_fallback=True, fallback_reason=f"setup: {e}")
    try:
        return _loop(run, t0)
    except Exception as e:  # last-resort net: still never raise
        return _fallback(run, t0, f"agent error: {e}")


def _load_run(order_id, draft_lines, db_path, rules) -> _Run:
    """Prefetch everything the model may need into one compact context."""
    conn = get_conn(db_path)
    try:
        order = conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise ValueError(f"Order #{order_id} not found.")
        c = conn.execute(
            "SELECT * FROM customers WHERE id = ?",
            (order["customer_id"],)).fetchone()
        basket = json.loads(c["usual_basket_json"])
        customer = {"name": c["name"], "credit_limit": c["credit_limit"],
                    "outstanding": c["outstanding"], "is_new": c["is_new"],
                    "usual_basket": basket}
        stock = {r["id"]: {"name": r["name"], "price": r["price"],
                           "stock_qty": r["stock_qty"]}
                 for r in conn.execute("SELECT * FROM items")}
        if draft_lines is None:  # fall back to the stored order lines
            draft_lines = [
                {"raw": f"{r['qty']:g} {r['name']}", "item_id": r["item_id"],
                 "item_name": r["name"], "qty": r["qty"], "unit": r["unit"],
                 "score": 100.0, "status": "ok", "candidates": []}
                for r in conn.execute(
                    "SELECT ol.qty, ol.item_id, i.name, i.unit FROM order_lines ol"
                    " JOIN items i ON i.id = ol.item_id WHERE ol.order_id = ?"
                    " ORDER BY ol.id", (order_id,))]
    finally:
        conn.close()
    catalog = ai_extract.load_catalog(db_path)
    return _Run(order_id, order["status"], customer, stock, catalog,
                [e["item"] for e in basket],
                [dict(ln) for ln in draft_lines], order["transcript"] or "",
                rules, db_path)


def _context_json(run: _Run) -> str:
    """Compact JSON facts for the model's first message."""
    return json.dumps({
        "order_id": run.order_id, "status": run.status,
        "transcript": run.transcript,
        "customer": {k: run.customer[k] for k in
                     ("name", "credit_limit", "outstanding", "is_new")},
        "usual_basket": run.usual_names,
        "draft": [{k: ln.get(k) for k in
                   ("raw", "item_id", "item_name", "qty", "unit", "status",
                    "candidates", "score")} for ln in run.draft],
        "stock": [{"item_id": i, "name": s["name"], "price": s["price"],
                   "in_stock": s["stock_qty"]}
                  for i, s in run.stock.items()
                  if i in {ln.get("item_id") for ln in run.draft}],
        "rulebook": {"action": run.latest.action,
                     "reasons": run.latest.reasons},
    }, ensure_ascii=False)


def _record(run: _Run, name: str, args: dict, result: dict, secs: float,
            history: list) -> None:
    """Trace one tool call, log its shortened result, feed it back to the model."""
    run.tool_calls.append({"name": name, "args": args,
                           "ok": bool(result.get("ok", False)),
                           "result": _short(result), "seconds": round(secs, 2)})
    run.steps.append(f"{name} -> {'ok' if result.get('ok') else 'error'}")
    _log(run.db_path, run.order_id, "agent_tool",
         f"Tool {name} {_short(args, 120)} -> {_short(result)}.",
         secs, {"tool": name})
    history.append({"role": "tool",
                    "content": json.dumps(result, ensure_ascii=False,
                                          default=str)})


def _loop(run: _Run, t0: float) -> AgentResult:
    """Up to MAX_TURNS model responses; propose_action finishes the run."""
    system = (ROOT / "prompts" / "agent_system.txt").read_text(encoding="utf-8")
    history = [{"role": "system", "content": system},
               {"role": "user",
                "content": "Order context (JSON):\n" + _context_json(run)}]
    seen: set[str] = set()
    handlers = {"get_customer_history": run.t_history,
                "search_catalog": run.t_search, "check_stock": run.t_check_stock,
                "resolve_line": run.t_resolve, "evaluate_rules": run.t_evaluate}
    for turn in range(MAX_TURNS):
        try:
            msg, _secs = llm.chat_with_tools(history, TOOLS)
        except Exception as e:  # timeout / LLMUnavailable / transport
            return _fallback(run, t0, f"llm failed: {e}")
        calls = msg.get("tool_calls") or []
        if not calls:
            return _fallback(run, t0, "model made no tool call")
        wire = []
        for call in calls:
            wire.append({"type": "function",
                         "function": {"name": call.get("name", ""),
                                      "arguments": call.get("arguments", {})}})
        history.append({"role": "assistant", "content": msg.get("content") or "",
                        "tool_calls": wire})
        run.steps.append(f"turn {turn + 1}: {len(calls)} tool call(s)")
        for call in calls:
            name, args = call.get("name", ""), call.get("arguments", {})
            if not isinstance(args, dict):
                return _fallback(run, t0, "bad tool arguments")
            key = json.dumps({"n": name, "a": args}, sort_keys=True, default=str)
            if key in seen:
                return _fallback(run, t0,
                                 f"repeated tool call: {name} {_short(args)}")
            seen.add(key)
            t1 = time.perf_counter()
            if run.status not in TOOL_STATES:
                result = {"ok": False,
                          "error": f"Tools are disabled in state {run.status}."}
                _record(run, name, args, result, time.perf_counter() - t1,
                        history)
                continue
            if name == "propose_action":
                return _finish(run, t0, args, time.perf_counter() - t1)
            if name not in handlers:
                return _fallback(run, t0, f"unknown tool: {name}")
            try:
                result = handlers[name](args)
            except Exception as e:
                result = {"ok": False, "error": f"Tool failed: {e}"}
            _record(run, name, args, result, time.perf_counter() - t1, history)
    return _fallback(run, t0, f"no proposal within {MAX_TURNS} turns")


def _ask_text(run: _Run, text: str) -> str:
    """Validate the model's ask_customer text, else pick the right template."""
    if text and len(text) <= messages.MAX_LEN and run.numbers_ok(text):
        return text
    open_line = next((ln for ln in run.draft if ln.get("item_id") is None), None)
    if open_line is not None:
        return messages.clarify_item(
            open_line.get("raw", ""), open_line.get("candidates", []))
    first = run.draft[0] if run.draft else {}
    usual = {e["item"]: e["qty"] for e in run.customer.get("usual_basket", [])}
    name = first.get("item_name", "order")
    return messages.confirm_unusual_qty(name, first.get("qty") or 0,
                                        usual.get(name, 0))


def _finish(run: _Run, t0: float, args: dict, secs: float) -> AgentResult:
    """Guardrail the proposal against the latest rulebook verdict."""
    kind = args.get("kind", "")
    if kind not in KIND_TO_ACTION:
        run.tool_calls.append({"name": "propose_action", "args": args,
                               "ok": False, "result": "bad kind",
                               "seconds": round(secs, 2)})
        history_note = f"propose_action bad kind {kind!r}"
        run.steps.append(history_note)
        _log(run.db_path, run.order_id, "agent_tool", history_note, secs,
             {"tool": "propose_action"})
        return _fallback(run, t0, f"bad proposal kind: {kind!r}")
    action = KIND_TO_ACTION[kind]
    if CAUTION.index(action) < CAUTION.index(run.latest.action):
        _log(run.db_path, run.order_id, "agent_overridden",
             f"Agent proposed {action}, rulebook says {run.latest.action}:"
             " rulebook wins.", secs,
             {"proposed": action, "rulebook": run.latest.action})
        final_kind, final_text = ACTION_TO_KIND[run.latest.action], ""
    else:
        final_kind = kind
        final_text = _ask_text(run, args.get("text", "")) \
            if kind == "ask_customer" else ""
    run.tool_calls.append({"name": "propose_action", "args": args, "ok": True,
                           "result": final_kind, "seconds": round(secs, 2)})
    run.steps.append(f"propose {kind} -> {final_kind}")
    _log(run.db_path, run.order_id, "agent_proposal",
         f"Agent proposes {final_kind}"
         + (f": {final_text}" if final_text else "") + ".",
         secs, {"kind": final_kind})
    return AgentResult(kind=final_kind, text=final_text, steps=run.steps,
                       tool_calls=run.tool_calls,
                       seconds=time.perf_counter() - t0)


def _fallback(run: _Run, t0: float, reason: str) -> AgentResult:
    """Deterministic rulebook decision, logged; the safe end of every failure."""
    _log(run.db_path, run.order_id, "agent_fallback",
         f"Agent fallback ({reason}); rulebook decides.", 0.0, {"reason": reason})
    kind = ACTION_TO_KIND[run.latest.action]
    return AgentResult(kind=kind, text="", steps=run.steps + [f"fallback: {reason}"],
                       tool_calls=run.tool_calls,
                       seconds=time.perf_counter() - t0,
                       used_fallback=True, fallback_reason=reason)
