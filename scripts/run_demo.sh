#!/bin/bash
# One-command demo: seed an isolated demo DB, prime scenes, start locally.
set -e
cd "$(dirname "$0")/.."
export OLLAMA_MAX_LOADED_MODELS=1 MUNSHI_DEMO=1 MUNSHI_AGENT=1
export MUNSHI_DB="${MUNSHI_DB:-data/demo.db}"
PYTHON="${PYTHON:-python3}"
if [ "$PYTHON" = "python3" ] && [ -x .venv/bin/python ]; then
  PYTHON=".venv/bin/python"
fi
MUNSHI_PORT="${MUNSHI_PORT:-8000}"
export MUNSHI_BASE_URL="${MUNSHI_BASE_URL:-http://localhost:${MUNSHI_PORT}}"

# Check before seeding: a failed second launch must not reset a live demo DB.
PORT_STATUS=$("$PYTHON" -c '
import socket
import sys

with socket.socket() as sock:
    try:
        sock.connect(("127.0.0.1", int(sys.argv[1])))
    except (ConnectionRefusedError, TimeoutError, OSError):
        print("free")
    else:
        print("busy")
' "$MUNSHI_PORT")
if [ "$PORT_STATUS" = "busy" ]; then
  echo "Munshi is already using 127.0.0.1:${MUNSHI_PORT}; demo data was not reset." >&2
  echo "Stop the running server with Ctrl+C, or choose another port:" >&2
  echo "  MUNSHI_PORT=8001 MUNSHI_MOCK_AI=1 bash scripts/run_demo.sh" >&2
  exit 1
fi

DEMO_AI="${MUNSHI_MOCK_AI:-1}"
MUNSHI_MOCK_AI=1 "$PYTHON" scripts/seed.py --db "$MUNSHI_DB" --single-shop
MUNSHI_MOCK_AI=1 "$PYTHON" scripts/demo_setup.py --db "$MUNSHI_DB"
export MUNSHI_MOCK_AI="$DEMO_AI"
exec "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port "$MUNSHI_PORT" \
  --no-access-log --log-level warning
