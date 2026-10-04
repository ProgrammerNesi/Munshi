# Munshi eval report
_Config: STT `mlx_whisper`, LLM `gemma4:e4b`, agent `on` · 2026-10-04T23:28:03 · 6 clips_

- Fully correct orders: **66.7%**
- Line-item accuracy: **75.0%**
- Qty accuracy: **75.0%**
- Average seconds per order: **16.2s**
  (act 0.0s, agent_proposal 0.0s, checked 0.0s, decision 6.73s, heard 1.38s, understood 8.12s)

## Transcripts

- `order1.wav` (customer 1): 2 kg, चीनी और 5 kg अत्ता.
- `order2.wav` (customer 3): 5-litre Sarsantel..
- `order3.wav` (customer 3): आध्धा kilo holdi.
- `order4.wav` (customer 2): 10 kg basmati chaval, 2 kg tour dal.
- `order5.wav` (customer 1): 100 kg, Cini.
- `order6.wav` (customer 5): 4 packet muggy.

## Failures

- `order4.wav` [NONE/CLARIFYING]: wrong item (expected item 5 qty 2)
- `order6.wav` [NONE/CLARIFYING]: wrong item (expected item 29 qty 4)
- `order6.wav` [NONE/CLARIFYING]: STT/extract error (needs_retype)

## Safety net

Caught by safety net: **2** (unbilled wrong orders: 2, agent_overridden events: 0).
A wrong extraction counts as caught when no bill went out for it (unusual-qty hold, clarifying question, approval hold or rejection).

## Agent

- Orders with agent runs: 4
- Without fallback: **100.0%**
- Average tool calls: **0.0**
- Average steps: **1.0** (agent_* events per agent-run order)
- Overridden proposals: **0**

## Comparison (STT backend x LLM model)

| STT backend | LLM model | agent | fully correct | line acc | qty acc | avg s/order | safety net |
|---|---|---|---|---|---|---|---|
| mlx_whisper | gemma4:e4b | on | 66.7% | 75.0% | 75.0% | 16.2s | 2 |

## Memory note (`ollama ps` at run time)

```
NAME          ID              SIZE      PROCESSOR    CONTEXT    UNTIL
gemma4:e4b    dc35e8d9c606    279 MB    100% GPU     8192       9 minutes from now
```
