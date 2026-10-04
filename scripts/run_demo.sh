#!/bin/bash
# One-command demo: clean data, three live scenes, start the app.
set -e
cd "$(dirname "$0")/.."
export OLLAMA_MAX_LOADED_MODELS=1 MUNSHI_DEMO=1 MUNSHI_AGENT=1
python3 scripts/reset_demo.py
python3 scripts/demo_setup.py
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
