#!/bin/bash
# ──────────────────────────────────────────────
#  CORTEX AI — Backend Startup Script
#  Dynamic port — auto-finds a free port
# ──────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
VENV_DIR="$BACKEND_DIR/venv"
SRC_DIR="$BACKEND_DIR/src"
API_FILE="$SRC_DIR/api.py"
PORT_FILE="$SCRIPT_DIR/.cortex_port"

echo "╔══════════════════════════════════════════════╗"
echo "║   CORTEX AI – BACKEND BOOT SEQUENCE          ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── 1. Activate virtualenv ─────────────────────
if [ -f "$VENV_DIR/bin/activate" ]; then
  echo "  [VENV]  Activating virtual environment..."
  source "$VENV_DIR/bin/activate"
else
  echo "  [WARN]  No venv found at $VENV_DIR"
  echo "          Attempting to use system Python3..."
fi

# ── 2. Verify api.py exists ────────────────────
if [ ! -f "$API_FILE" ]; then
  echo "  [ERR]   Cannot find api.py at $API_FILE"
  exit 1
fi

# ── 3. Load .env if present ────────────────────
ENV_FILE="$BACKEND_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  echo "  [ENV]   Loading .env..."
  set -a; source "$ENV_FILE"; set +a
fi

# ── 4. Install / update dependencies ──────────
REQ_FILE="$BACKEND_DIR/requirements.txt"
if [ -f "$REQ_FILE" ]; then
  echo "  [DEPS]  Installing dependencies (quiet)..."
  pip install -q -r "$REQ_FILE" 2>&1 | grep -v "already satisfied" || true
fi

# ── 5. Set PYTHONPATH so relative imports work ─
export PYTHONPATH="$SRC_DIR:${PYTHONPATH:-}"
echo "  [PATH]  PYTHONPATH=$PYTHONPATH"

# ── 6. Dynamic port selection ──────────────────
#    Tries preferred ports in order. If all busy,
#    asks the OS for a free port. Writes result to
#    .cortex_port so the frontend can discover it.
PREFERRED_PORTS=(8100 8000 3000 5000 5001 8080 9000 4000 8200 8888)
CHOSEN_PORT=""

# Allow user to force a port via env var or argument
if [ -n "${CORTEX_PORT:-}" ]; then
  CHOSEN_PORT="$CORTEX_PORT"
  echo "  [PORT]  Using CORTEX_PORT=$CHOSEN_PORT (env override)"
elif [ -n "${1:-}" ]; then
  CHOSEN_PORT="$1"
  echo "  [PORT]  Using port $CHOSEN_PORT (CLI argument)"
fi

# Auto-detect a free port from the preferred list
if [ -z "$CHOSEN_PORT" ]; then
  for port in "${PREFERRED_PORTS[@]}"; do
    # Check if port is free (works on macOS and Linux)
    if ! lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      CHOSEN_PORT="$port"
      break
    else
      echo "  [PORT]  :$port busy — skipping"
    fi
  done
fi

# Last resort: ask Python for a random free port
if [ -z "$CHOSEN_PORT" ]; then
  CHOSEN_PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")
  echo "  [PORT]  All preferred ports busy — OS assigned :$CHOSEN_PORT"
fi

# Write chosen port to .cortex_port (frontend reads this)
echo "$CHOSEN_PORT" > "$PORT_FILE"
export CORTEX_PORT="$CHOSEN_PORT"

# Cleanup .cortex_port on exit
trap "rm -f '$PORT_FILE'" EXIT INT TERM

# ── 7. Launch uvicorn ──────────────────────────
echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │  🚀 Cortex API → http://0.0.0.0:$CHOSEN_PORT    "
echo "  │  📁 Port file  → .cortex_port           │"
echo "  │  Press Ctrl+C to stop                    │"
echo "  └─────────────────────────────────────────┘"
echo ""

cd "$SRC_DIR"
exec uvicorn api:app \
  --host 0.0.0.0 \
  --port "$CHOSEN_PORT" \
  --reload \
  --log-level info
