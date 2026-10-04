# Munshi: project context for the coding agent

Munshi is a local, open-source AI clerk for a small Indian wholesale/kirana shop.
Customers send voice notes/text in Hindi/English/Hinglish. Munshi turns them into
orders, bills, packing tasks and deliveries, and only asks the owner when needed.

## Principles
1. The AI investigates and proposes; the engine commits; humans handle exceptions.
   The LLM may: (a) transcribe audio (optional backend), (b) extract an order from a transcript,
   (c) in the agent loop, call read-only tools and propose an action, (d) write short friendly text,
   (e) write the daily briefing.
   Money, approval, stock, billing and dispatch decisions are deterministic Python reading rules.yaml.
   The LLM can never approve credit or override a rule. A proposal less cautious than the rulebook
   is overridden and logged.
   Caution order: AUTO_CONFIRM < ASK_CUSTOMER < ASK_OWNER < REJECT_SHORT_STOCK.
2. Local and offline at runtime: no cloud APIs, no CDN assets. Any optional cloud backend must be
   off by default, behind an env flag, and labelled in the UI.
3. Every step writes a row to the `events` table (actor, kind, message, data_json including seconds)
   so the UI can show a human-readable reasoning log.
4. Simple beats clever. One repo, one process, `uvicorn app.main:app`. No Docker, no React, no ORM.
5. MUNSHI_MOCK_AI=1 replaces STT/LLM with canned outputs so UI work and tests never wait on models.
6. Always have a fallback. If the agent loop, an STT backend or the LLM fails, fall back to the
   deterministic path and log it. Never crash and never leave an order silently stuck.

## Models (MacBook Air M5, 16GB: keep it light)
- LLM and agent: Gemma 4 E4B via Ollama (tag gemma4:e4b). num_ctx 8192, temperature 0.
- STT default: mlx-whisper with mlx-community/whisper-large-v3-turbo.
  Alternate backend: Gemma 4 E4B audio input via Ollama.
- Do NOT use faster-whisper (CPU-only on Mac).
- The pipeline is sequential (STT, then LLM), and only one Ollama model is loaded at a time
  (OLLAMA_MAX_LOADED_MODELS=1).

## Env vars
MUNSHI_MOCK_AI (0/1) · MUNSHI_STT_BACKEND (mlx_whisper|gemma_audio|mock) · MUNSHI_STT_MODEL ·
MUNSHI_STT_LANG (default auto) · MUNSHI_LLM_MODEL (default gemma4:e4b) · MUNSHI_NUM_CTX (8192) ·
MUNSHI_KEEP_ALIVE (10m) · MUNSHI_AGENT (0/1, default 1) · MUNSHI_DEMO (0/1)

## Stack
Python 3.11, FastAPI, Jinja2 templates, vanilla JS (fetch polling every 2s), plain CSS,
sqlite3 (stdlib), mlx-whisper, ffmpeg, Ollama HTTP API (localhost:11434), rapidfuzz, pydantic,
PyYAML, pytest.

## Layout
app/main.py (routes) · app/db.py · app/engine.py (state machine) · app/rules.py
app/pipeline.py (orchestration) · app/agent.py (agent loop) · app/bill.py · app/messages.py
app/ai/{audio,stt,llm,extract,briefing}.py · app/templates/ · app/static/
scripts/{seed,reset_demo,smoke_models,try_order,run_eval}.py
prompts/{extract.txt,agent_system.txt,briefing.txt} · data/ · eval/ · tests/ · rules.yaml · PROMPTS.md

## Working rules
- Read this file before every task. Work one phase at a time; do not build ahead.
- After each phase: run tests, run the app, and print how to verify manually.
- Mobile-first CSS for packer/delivery pages. Big tap targets. Hinglish-friendly UI text.
- Never invent data silently: seeded or fake data must be clearly marked in the seed scripts.
- Keep functions small and commented. Prefer boring code.
- mlx-whisper only installs on Apple Silicon: import it lazily so tests run anywhere.
- Never call datetime.now() directly; use app/clock.py now(). In demo mode (MUNSHI_DEMO=1) the clock can be advanced and every portal shows a "DEMO CLOCK +N min" badge.
- The supervisor is deterministic. It detects delays from rules.yaml SLAs and acts only through engine functions. No LLM is involved. Messages are templates.
- Tracking links use unguessable tokens and never expose order ids, staff phones, other customers, balances or credit limits.