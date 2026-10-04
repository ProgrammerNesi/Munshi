"""Transcript -> DraftOrder: LLM extracts, deterministic code matches.

The LLM only turns speech into {raw, item_guess, qty, unit} lines (validated
with pydantic). Matching those guesses to catalog items, unit checks and all
thresholds below are plain Python + rapidfuzz — never the model.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ValidationError
from rapidfuzz import fuzz

from app.ai import llm
from app.ai.llm import LLMBadOutput, LLMUnavailable
from app.db import get_conn

ROOT = Path(__file__).resolve().parent.parent.parent  # app/ai/ -> repo root

# -- thresholds (the only knobs; plain numbers, no ML) ----------------------
MATCH_MIN_SCORE = 88  # accept a match only at/above this rapidfuzz score
MATCH_MARGIN = 8  # winner must beat the runner-up by this much ...
MAX_CANDIDATES = 3  # ... else the line is "unresolved" with top-3 candidates
LLM_TRIES = 2  # JSON parse attempts before asking the customer to retype
NUMBER_WORDS = {
    "one": 1, "ek": 1, "a": 1, "two": 2, "do": 2, "three": 3, "teen": 3,
    "four": 4, "char": 4, "chaar": 4, "five": 5, "paanch": 5,
    "six": 6, "chhe": 6, "seven": 7, "saat": 7, "eight": 8, "aath": 8,
    "nine": 9, "nau": 9, "ten": 10, "das": 10, "half": 0.5,
    "aadha": 0.5, "dedh": 1.5, "dhai": 2.5,
}
UNIT_ALIASES = {
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg",
    "kilogram": "kg", "kilograms": "kg",
    "g": "g", "gram": "g", "grams": "g",
    "litre": "litre", "litres": "litre", "liter": "litre",
    "liters": "litre", "ltr": "litre",
    "pack": "pack", "packs": "pack", "packet": "pack", "packets": "pack",
    "pkt": "pack", "box": "box", "boxes": "box",
    "piece": "piece", "pieces": "piece", "pc": "piece",
    "dozen": "dozen", "bori": "bori", "katta": "bori",
}
_NUMBER_PATTERN = "|".join(
    re.escape(word) for word in sorted(NUMBER_WORDS, key=len, reverse=True))
_QUANTITY_RE = re.compile(
    rf"(?<!\w)(\d+(?:\.\d+)?|{_NUMBER_PATTERN})"
    rf"(?:\s+(kg|kgs|kilo|kilos|kilograms?|g|grams?|litres?|liters?|ltr|"
    rf"packs?|packets?|pkt|boxes?|box|pieces?|pc|dozen|bori|katta))?"
    rf"(?!\w)",
    re.IGNORECASE,
)
_FILLER_WORDS = {
    "please", "send", "bhej", "bhejo", "bhejna", "dena", "dijiye",
    "deliver", "delivery", "order", "chahiye", "de", "do", "ji", "bhaiya",
    "bhai", "mere", "pass", "kar", "dijiye", "wala", "wali", "wale",
}

# Line statuses.
OK = "ok"
UNRESOLVED = "unresolved"
UNIT_UNCLEAR = "unit_unclear"  # explicit unit != the item's selling unit
UNIT_ASSUMED = "unit_assumed"  # no unit said -> selling unit assumed
QTY_MISSING = "qty_missing"


# -- LLM payload shape -------------------------------------------------------

class ExtractLine(BaseModel):
    """One line exactly as the prompt contract promises."""

    raw: str
    item_guess: str
    qty: float | None
    unit: str


class ExtractPayload(BaseModel):
    """Whole-model-output shape (also sent as Ollama's format schema)."""

    lines: list[ExtractLine]
    notes: str = ""


EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw": {"type": "string"},
                    "item_guess": {"type": "string"},
                    "qty": {"type": ["number", "null"]},
                    "unit": {"type": "string"},
                },
                "required": ["raw", "item_guess", "qty", "unit"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["lines", "notes"],
}


# -- draft output ------------------------------------------------------------

@dataclass
class DraftLine:
    """One matched line: item_id None + candidates when unresolved."""

    raw: str
    item_id: int | None
    item_name: str
    qty: float | None
    unit: str
    score: float
    status: str
    candidates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-safe form for logs, UI and try_order output."""
        return {
            "raw": self.raw, "item_id": self.item_id, "item_name": self.item_name,
            "qty": self.qty, "unit": self.unit, "score": round(self.score, 1),
            "status": self.status, "candidates": self.candidates,
        }


@dataclass
class DraftOrder:
    """Extraction result. needs_retype=True: show the customer a retry prompt."""

    lines: list[DraftLine]
    notes: str
    transcript: str
    needs_retype: bool = False

    def to_dict(self) -> dict:
        """JSON-safe form for logs, UI and try_order output."""
        return {
            "lines": [ln.to_dict() for ln in self.lines],
            "notes": self.notes, "transcript": self.transcript,
            "needs_retype": self.needs_retype,
        }


# -- matching ----------------------------------------------------------------

def normalise(text: str) -> str:
    """NFKC + lowercase + drop punctuation/symbols + single spaces.

    Punctuation is detected by Unicode category (not \\w), so Devanagari
    vowel signs and other combining marks survive in both scripts.
    """
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = "".join(
        ch for ch in text
        if not unicodedata.category(ch).startswith(("P", "S")) or ch.isspace()
    )
    return re.sub(r"\s+", " ", text).strip()


def load_catalog(db_path=None) -> list[dict]:
    """Catalog rows as matcher entries: item_id, name, unit, aliases."""
    conn = get_conn(db_path)
    try:
        return [
            {"item_id": r["id"], "name": r["name"], "unit": r["unit"],
             "aliases": json.loads(r["aliases_json"])}
            for r in conn.execute("SELECT id, name, aliases_json, unit FROM items")
        ]
    finally:
        conn.close()


def _score_guess(guess: str, entry: dict) -> float:
    """Best rapidfuzz score of the guess against the name and every alias."""
    return max(
        [fuzz.WRatio(guess, normalise(entry["name"]))] +
        [fuzz.WRatio(guess, normalise(a)) for a in entry["aliases"]] or [0.0]
    )


def match_item(guess: str, catalog: list[dict],
               usual_names: list[str] | None = None) -> tuple[
                   int | None, str, float, bool, list[str]]:
    """Match one guess. Returns (item_id, name, score, resolved, candidates).

    Accepted when score >= MATCH_MIN_SCORE and the margin over the runner-up
    is >= MATCH_MARGIN. The usual basket breaks exact ties only: if the top
    two are within margin and exactly one is usual, take it.
    """
    guess = normalise(guess)
    ranked = sorted(
        ((e, _score_guess(guess, e)) for e in catalog),
        key=lambda pair: pair[1], reverse=True,
    )
    top3 = [e["name"] for e, _ in ranked[:MAX_CANDIDATES]]
    if not ranked:
        return None, guess, 0.0, False, []
    (best, best_score) = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score >= MATCH_MIN_SCORE and best_score - second_score >= MATCH_MARGIN:
        return best["item_id"], best["name"], best_score, True, []
    if (usual_names and len(ranked) > 1
            and best_score - second_score < MATCH_MARGIN
            and best_score >= MATCH_MIN_SCORE):
        top2 = [ranked[0][0]["name"], ranked[1][0]["name"]]
        usual_top2 = [n for n in top2 if n in usual_names]
        if len(usual_top2) == 1:  # tie broken by habit, nothing else
            pick = ranked[0][0] if ranked[0][0]["name"] == usual_top2[0] else ranked[1][0]
            pick_score = ranked[0][1] if pick is ranked[0][0] else ranked[1][1]
            return pick["item_id"], pick["name"], pick_score, True, []
    return None, guess, best_score, False, top3


def _build_prompt(transcript: str, catalog: list[dict],
                  usual_names: list[str] | None) -> str:
    """Fill prompts/extract.txt placeholders with plain str.replace.

    (str.format is banned here: the template itself contains JSON braces.)
    """
    template = (ROOT / "prompts" / "extract.txt").read_text(encoding="utf-8")
    hints = "; ".join(
        f"{e['name']} ({', '.join(e['aliases'])})" for e in catalog)
    return (
        template
        .replace("{catalog_names_and_aliases}", hints)
        .replace("{usual_basket}", ", ".join(usual_names or ["none"]))
        .replace("{transcript}", transcript)
    )


def _llm_lines(prompt: str) -> ExtractPayload | None:
    """One prompt shape, up to LLM_TRIES parses. None = ask for a retype."""
    for _ in range(LLM_TRIES):
        try:
            obj, _seconds = llm.generate_json(prompt, EXTRACT_SCHEMA)
            return ExtractPayload.model_validate(obj)
        except (LLMBadOutput, ValidationError, LLMUnavailable):
            continue
    return None


def _parse_quantity(clause: str) -> tuple[float | None, str]:
    """Read an explicit quantity and unit from one conjunction-separated item."""
    for match in _QUANTITY_RE.finditer(clause):
        suffix = clause[match.end():].lstrip().lower()
        if suffix.startswith(("wala", "wali", "wale")):
            continue
        raw_qty = match.group(1).lower()
        qty = float(raw_qty) if raw_qty[0].isdigit() else NUMBER_WORDS[raw_qty]
        unit = UNIT_ALIASES.get((match.group(2) or "").lower(), "unknown")
        return qty, unit
    return None, "unknown"


def _guess_unknown_item(clause: str) -> str:
    """Remove quantities and common order words, leaving the spoken item name."""
    words = []
    for word in normalise(_QUANTITY_RE.sub(" ", clause)).split():
        if word not in _FILLER_WORDS and not word.isdigit():
            words.append(word)
    return " ".join(words)


def _simple_text_order(transcript: str, catalog: list[dict],
                       usual_names: list[str] | None) -> DraftOrder:
    """Conservative no-model parser for clear typed lines and mock demos."""
    clauses = re.split(r"\s*(?:,|;|&|\band\b|\baur\b)\s*",
                       transcript, flags=re.IGNORECASE)
    lines = []
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        qty, unit = _parse_quantity(clause)
        normalized = normalise(clause)
        hits: dict[int, tuple[dict, str]] = {}
        for entry in catalog:
            for alias in [entry["name"], *entry["aliases"]]:
                name = normalise(alias)
                if name and name in normalized:
                    previous = hits.get(entry["item_id"])
                    if previous is None or len(name) > len(previous[1]):
                        hits[entry["item_id"]] = (entry, name)

        if len(hits) == 1:
            entry = next(iter(hits.values()))[0]
            lines.append(DraftLine(
                raw=clause, item_id=entry["item_id"], item_name=entry["name"],
                qty=qty, unit=unit, score=100.0,
                status=QTY_MISSING if qty is None else OK,
            ))
            continue

        guess = _guess_unknown_item(clause)
        if hits:
            guess = max((name for _entry, name in hits.values()), key=len)
        if not guess:
            continue
        item_id, name, score, resolved, candidates = match_item(
            guess, catalog, usual_names)
        entry = next((e for e in catalog if e["item_id"] == item_id), None)
        lines.append(DraftLine(
            raw=clause, item_id=item_id if resolved else None,
            item_name=entry["name"] if entry else name, qty=qty,
            unit=unit, score=score,
            status=(QTY_MISSING if resolved and qty is None else
                    OK if resolved else UNRESOLVED),
            candidates=candidates,
        ))

    return DraftOrder(
        lines=lines, notes="", transcript=transcript,
        needs_retype=not lines,
    )


def extract_order(transcript: str, usual_names: list[str] | None = None,
                  catalog: list[dict] | None = None, db_path=None) -> DraftOrder:
    """Transcript -> DraftOrder. Never raises for model-side trouble."""
    catalog = catalog if catalog is not None else load_catalog(db_path)
    payload = _llm_lines(_build_prompt(transcript, catalog, usual_names))
    if payload is None or not payload.lines:
        fallback = _simple_text_order(transcript, catalog, usual_names)
        if fallback.lines or payload is None:
            return fallback
        return DraftOrder(lines=[], notes="", transcript=transcript,
                          needs_retype=True)
    lines = []
    for ln in payload.lines:
        item_id, name, score, resolved, candidates = match_item(
            ln.item_guess, catalog, usual_names)
        if not resolved:
            lines.append(DraftLine(ln.raw, None, ln.item_guess or ln.raw,
                                   ln.qty, ln.unit, score, UNRESOLVED, candidates))
            continue
        entry = next(e for e in catalog if e["item_id"] == item_id)
        unit, status = ln.unit, OK
        if ln.qty is None:
            status = QTY_MISSING
        elif ln.unit == "unknown":
            unit, status = entry["unit"], UNIT_ASSUMED
        elif ln.unit != entry["unit"]:
            status = UNIT_UNCLEAR
        lines.append(DraftLine(ln.raw, item_id, name, ln.qty, unit,
                               score, status, []))
    return DraftOrder(lines=lines, notes=payload.notes,
                      transcript=transcript, needs_retype=False)
