# Munshi — local AI clerk for a wholesale kirana shop

Customers send voice notes or text in Hindi/English/Hinglish. Munshi turns
them into orders, bills, packing tasks and deliveries — and only asks the
owner when money or judgment is involved. Everything runs on one laptop:
no cloud, no accounts, one `uvicorn` process.

![customer chat](docs/customer.png) ![owner board](docs/owner.png)
*(screenshots placeholder — drop real captures in `docs/`)*

## One-command run

```bash
brew install ffmpeg                          # mic/audio support
ollama pull gemma4:e4b                       # the local LLM
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
bash scripts/run_demo.sh                     # reset data, prime 3 demo scenes, serve on :8000
```

Open http://127.0.0.1:8000 — pick Customer, Owner, Packer or Delivery.
The demo script leaves three live scenes: an auto-confirmed order, a
credit approval waiting in `/owner`, and a pack mismatch to resolve.

Without the demo data: `python scripts/seed.py` (30 days of labelled fake
history) and `uvicorn app.main:app --port 8000` yourself.

## Mac setup

- Apple Silicon Mac, Python 3.11+, `brew install ffmpeg`
- Ollama updated past the audio-transcriptions API if you use the
  `gemma_audio` STT backend (`ollama pull gemma4:e4b`)
- `mlx-whisper` installs from `requirements.txt` on macOS only and is
  imported lazily, so tests also run on other machines
- Keep `OLLAMA_MAX_LOADED_MODELS=1`: the pipeline is STT-then-LLM and only
  one model is loaded at a time

## Env vars

| Var | Default | What it does |
|---|---|---|
| `MUNSHI_MOCK_AI` | `0` | `1` = canned STT/LLM outputs, tests and UI never wait on models |
| `MUNSHI_STT_BACKEND` | `mlx_whisper` | `mlx_whisper` \| `gemma_audio` \| `mock` (falls back once on failure) |
| `MUNSHI_STT_MODEL` | `mlx-community/whisper-large-v3-turbo` | HuggingFace model for mlx-whisper |
| `MUNSHI_STT_LANG` | `auto` | transcribe language hint |
| `MUNSHI_LLM_MODEL` | `gemma4:e4b` | Ollama tag for extract/agent/chat/briefing |
| `MUNSHI_NUM_CTX` | `8192` | LLM context window |
| `MUNSHI_KEEP_ALIVE` | `10m` | keep the model loaded between requests |
| `MUNSHI_AGENT` | `1` | `0` = skip the agent loop, pure rulebook path |
| `MUNSHI_DEMO` | `0` | `1` = demo banners/labels in the UI |
| `MUNSHI_SCHEDULER` | `1` | `0` = disable the daily 8:00 briefing pre-warm |

## The rulebook (`rules.yaml`)

The owner edits numbers here, never code — and the LLM can never overrule
them. Fewer, dumber, auditable:

- **credit**: order waits for the owner when outstanding + bill crosses the limit
- **order**: auto-confirm cap (₹15,000) and unusual-qty multiple (3× usual basket)
- **customers**: new customers always need approval
- **stock**: short stock rejects by default (no silent partials)
- **packing**: any pack mismatch locks dispatch and notifies owner + packer
- **delivery**: ₹40 fee, free above ₹2,000 · watchdog nudges past 20/60 min

A proposal less cautious than the rulebook is overridden and logged
(`agent_overridden`), most-cautious-first:
`AUTO_CONFIRM < ASK_CUSTOMER < ASK_OWNER < REJECT_SHORT_STOCK`.

## Honest limitations

- **No auth.** Anyone on the laptop/network can open `/owner` and approve
  credit. Local demo tool, not production software.
- **Simulated WhatsApp.** The customer page looks like a chat app but is a
  local web page; no real WhatsApp integration.
- **Seeded history.** The 30-day order history is synthetic demo data from
  `scripts/seed.py` (marked `[DEMO HISTORY]` in transcripts), not real sales.
- **The agent can fall back.** When the loop fails it silently takes the
  deterministic path and logs `agent_fallback` — check the owner timeline
  if the assistant feels "dumb".
- **Models tested on one Mac only** (M-series, 16GB). Whisper mistranscribes
  some words (`eval/report.md`: "tour dal", "muggy"); the clarify/mismatch
  safety nets exist for exactly this.
- **Single worker, SQLite.** One message at a time; fine for a demo shop,
  not for real concurrency.

## Known bugs

- `eval/report.md` may show a duplicate-looking `needs_retype` + `wrong
  item` pair for the same clip: both symptoms are listed, root cause is one
  bad transcription.
- The packer page skips re-rendering while a count field is focused, so a
  second packer's submit can briefly show a stale list (refreshes on next poll).
- Packer/delivery have no login or per-staffer assignment view; tasks are
  pooled for whoever opens the page.
- Audio uploads stay in `data/uploads/` forever; nothing cleans them up yet.
