"""Extraction tests with a fake LLM (no models, no network, hermetic catalog)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai import extract as ai_extract  # noqa: E402
from app.ai.llm import LLMBadOutput  # noqa: E402

# Small hermetic catalog (mirrors data/catalog.csv entries used here).
TEST_CATALOG = [
    {"item_id": 1, "name": "Sugar", "unit": "kg",
     "aliases": ["cheeni", "chini", "shakkar", "sugar", "khand", "चीनी", "शक्कर"]},
    {"item_id": 2, "name": "Wheat Flour (Atta)", "unit": "kg",
     "aliases": ["atta", "gehu atta", "wheat flour", "aata", "आटा", "गेहूं आटा"]},
    {"item_id": 12, "name": "Mustard Oil", "unit": "litre",
     "aliases": ["sarso tel", "sarson tel", "mustard oil", "sarso oil",
                 "kacha tel", "सरसों तेल", "तेल"]},
    {"item_id": 13, "name": "Refined Sunflower Oil", "unit": "litre",
     "aliases": ["refined", "refined tel", "sunflower oil", "refined oil",
                 "safola", "रिफाइंड तेल", "तेल"]},
    {"item_id": 14, "name": "Groundnut Oil", "unit": "litre",
     "aliases": ["moongfali tel", "groundnut oil", "peanut oil", "sing tel",
                 "मूंगफली तेल", "तेल"]},
    {"item_id": 22, "name": "Turmeric Powder (Haldi)", "unit": "kg",
     "aliases": ["haldi", "turmeric", "haldi powder", "हल्दी"]},
    {"item_id": 15, "name": "Tea (Chai Patti)", "unit": "pack",
     "aliases": ["chai", "chai patti", "tea", "chay", "patti", "चाय", "चाय पत्ती"]},
    {"item_id": 28, "name": "Biscuits (Parle-G)", "unit": "pack",
     "aliases": ["biscuit", "parle", "parle-g", "glucose biscuit", "biskit",
                 "बिस्कुट", "पारले"]},
    {"item_id": 29, "name": "Maggi Noodles", "unit": "pack",
     "aliases": ["maggi", "noodles", "instant noodles", "मैगी", "नूडल्स"]},
    {"item_id": 16, "name": "Salt (Tata Namak)", "unit": "kg",
     "aliases": ["namak", "salt", "tata namak", "tata salt", "नमक", "टाटा नमक"]},
]


def _llm_with(payload, monkeypatch):
    """Fake the LLM returning one prepared JSON payload (with seconds)."""
    monkeypatch.setattr("app.ai.llm.generate_json",
                        lambda prompt, schema, **k: (payload, 0.01))


def _extract(transcript, payload, monkeypatch, **kw):
    _llm_with(payload, monkeypatch)
    return ai_extract.extract_order(transcript, catalog=TEST_CATALOG, **kw)


def _line(raw, guess, qty, unit):
    return {"raw": raw, "item_guess": guess, "qty": qty, "unit": unit}


# -- 10 transcripts ----------------------------------------------------------

def test_roman_simple_cheeni(monkeypatch):
    d = _extract("bhaiya do kilo cheeni bhej dena",
                 {"lines": [_line("do kilo cheeni", "cheeni", 2, "kg")], "notes": ""},
                 monkeypatch)
    assert not d.needs_retype
    (ln,) = d.lines
    assert (ln.item_id, ln.qty, ln.unit, ln.status) == (1, 2, "kg", "ok")


def test_roman_two_lines_atta_namak(monkeypatch):
    d = _extract("paanch kilo atta aur ek kilo namak",
                 {"lines": [_line("paanch kilo atta", "atta", 5, "kg"),
                             _line("ek kilo namak", "namak", 1, "kg")], "notes": ""},
                 monkeypatch)
    assert [(ln.item_id, ln.status) for ln in d.lines] == [(2, "ok"), (16, "ok")]


def test_roman_sarson_tel_litre(monkeypatch):
    d = _extract("do litre sarson tel",
                 {"lines": [_line("do litre sarson tel", "sarson tel", 2, "litre")],
                  "notes": ""},
                 monkeypatch)
    (ln,) = d.lines
    assert (ln.item_id, ln.status) == (12, "ok")


def test_roman_pack_items(monkeypatch):
    d = _extract("do pack maggi aur ek pack biscuit",
                 {"lines": [_line("do pack maggi", "maggi", 2, "pack"),
                             _line("ek pack biscuit", "biscuit", 1, "pack")],
                  "notes": ""},
                 monkeypatch)
    assert [(ln.item_id, ln.status) for ln in d.lines] == [(29, "ok"), (28, "ok")]


def test_roman_chai_notes_passthrough(monkeypatch):
    d = _extract("paanch chai patti kal subah",
                 {"lines": [_line("paanch chai patti", "chai patti", 5, "pack")],
                  "notes": "kal subah"},
                 monkeypatch)
    (ln,) = d.lines
    assert (ln.item_id, ln.status, d.notes) == (15, "ok", "kal subah")


def test_roman_bori_unit_mismatch(monkeypatch):
    d = _extract("do bori cheeni",
                 {"lines": [_line("do bori cheeni", "cheeni", 2, "bori")], "notes": ""},
                 monkeypatch)
    (ln,) = d.lines
    assert (ln.item_id, ln.unit, ln.status) == (1, "bori", "unit_unclear")


def test_devanagari_haldi(monkeypatch):
    d = _extract("आधा किलो हल्दी",
                 {"lines": [_line("आधा किलो हल्दी", "हल्दी", 0.5, "kg")], "notes": ""},
                 monkeypatch)
    (ln,) = d.lines
    assert (ln.item_id, ln.qty, ln.status) == (22, 0.5, "ok")


def test_devanagari_sarson_tel(monkeypatch):
    d = _extract("दो लीटर सरसों तेल",
                 {"lines": [_line("दो लीटर सरसों तेल", "सरसों तेल", 2, "litre")],
                  "notes": ""},
                 monkeypatch)
    (ln,) = d.lines
    assert (ln.item_id, ln.status) == (12, "ok")


def test_unknown_item_unresolved(monkeypatch):
    d = _extract("do kilo quinoa",
                 {"lines": [_line("do kilo quinoa", "quinoa", 2, "kg")], "notes": ""},
                 monkeypatch)
    (ln,) = d.lines
    assert ln.status == "unresolved" and ln.item_id is None
    assert len(ln.candidates) == 3


def test_missing_qty(monkeypatch):
    d = _extract("cheeni bhej dena",
                 {"lines": [_line("cheeni", "cheeni", None, "unknown")], "notes": ""},
                 monkeypatch)
    (ln,) = d.lines
    assert (ln.item_id, ln.qty, ln.status) == (1, None, "qty_missing")


def test_unknown_unit_assumed(monkeypatch):
    d = _extract("do cheeni",
                 {"lines": [_line("do cheeni", "cheeni", 2, "unknown")], "notes": ""},
                 monkeypatch)
    (ln,) = d.lines
    assert (ln.item_id, ln.unit, ln.status) == (1, "kg", "unit_assumed")


# -- matcher unit tests (no LLM at all) ---------------------------------------

def test_matcher_roman_alias():
    item_id, name, score, resolved, _ = ai_extract.match_item("chini", TEST_CATALOG)
    assert (item_id, resolved) == (1, True) and score >= 88


def test_matcher_devanagari_alias():
    item_id, name, score, resolved, _ = ai_extract.match_item("हल्दी", TEST_CATALOG)
    assert (item_id, name, resolved) == (22, "Turmeric Powder (Haldi)", True)


def test_matcher_ambiguous_tel_unresolved():
    item_id, _name, score, resolved, candidates = ai_extract.match_item(
        "tel", TEST_CATALOG)
    assert resolved is False and item_id is None and score >= 88
    assert candidates == ["Mustard Oil", "Refined Sunflower Oil", "Groundnut Oil"]


def test_matcher_usual_basket_breaks_tie_only():
    # "tel" ties across the three oils; the usual one wins.
    item_id, name, _s, resolved, _c = ai_extract.match_item(
        "tel", TEST_CATALOG, usual_names=["Refined Sunflower Oil"])
    assert (item_id, name, resolved) == (13, "Refined Sunflower Oil", True)
    # Two usuals in the top two = still a tie = unresolved.
    item_id, _n, _s, resolved, _c = ai_extract.match_item(
        "tel", TEST_CATALOG,
        usual_names=["Refined Sunflower Oil", "Mustard Oil"])
    assert (item_id, resolved) == (None, False)


def test_prompt_placeholders_filled_without_format():
    prompt = ai_extract._build_prompt("do kilo cheeni", TEST_CATALOG, ["Sugar"])
    assert "do kilo cheeni" in prompt and "cheeni" in prompt
    assert (("{transcript}" not in prompt)
            and ("{catalog_names_and_aliases}" not in prompt)
            and ("{usual_basket}" not in prompt))
    assert '"qty"' in prompt  # template JSON braces survived intact


# -- failure path --------------------------------------------------------------

def test_bad_json_twice_needs_retype(monkeypatch):
    def _boom(prompt, schema, **k):
        raise LLMBadOutput("not json")
    monkeypatch.setattr("app.ai.llm.generate_json", _boom)
    d = ai_extract.extract_order("do kilo cheeni", catalog=TEST_CATALOG)
    assert d.needs_retype is True and d.lines == []
