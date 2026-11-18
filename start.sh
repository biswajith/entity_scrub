#!/usr/bin/env bash
# Create the virtualenv (first run only) and start the API + UI.
#
#   ./start.sh                 # install if needed, then run
#   ./start.sh --reinstall     # rebuild the venv from scratch
#
# Env overrides: API_PORT (8765), UI_PORT (8501), SCRUB_DEVICE (cpu|mps), SCRUB_HOME, SCRUB_MODEL
set -euo pipefail

cd "$(dirname "$0")"
API_PORT="${API_PORT:-8765}"
UI_PORT="${UI_PORT:-8501}"
VENV=".venv"

log() { printf '\033[1;34m[start]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[start]\033[0m %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--reinstall" ]]; then
  log "Removing $VENV"
  rm -rf "$VENV"
fi

# ---------------------------------------------------------------------------
# 1. virtualenv + dependencies
# ---------------------------------------------------------------------------
install_with_uv() {
  log "Installing with uv (creates $VENV)…"
  uv sync --python 3.12
}

find_python() {
  for py in python3.12 python3.11 python3; do
    if command -v "$py" >/dev/null 2>&1; then
      if "$py" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 13) else 1)' 2>/dev/null; then
        echo "$py"; return 0
      fi
    fi
  done
  return 1
}

install_with_pip() {
  local py
  py="$(find_python)" || die "Need Python 3.11 or 3.12 (or install uv: https://docs.astral.sh/uv/)"
  log "Creating $VENV with $py…"
  "$py" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
  if [[ "$(uname -s)" == "Linux" ]]; then
    # CPU-only torch on Linux (much smaller than the CUDA build)
    "$VENV/bin/pip" install torch --index-url https://download.pytorch.org/whl/cpu
  fi
  log "Installing project dependencies…"
  "$VENV/bin/pip" install -e . pytest httpx openpyxl python-docx
}

needs_install() {
  [[ ! -x "$VENV/bin/python" ]] && return 0
  "$VENV/bin/python" -c 'import gliner, fastapi, streamlit, pymupdf, scrub' >/dev/null 2>&1 && return 1
  return 0
}

if needs_install; then
  if command -v uv >/dev/null 2>&1; then
    install_with_uv
  else
    install_with_pip
  fi
else
  log "Using existing $VENV"
fi

PY="$VENV/bin/python"

# ---------------------------------------------------------------------------
# 2. start the API, wait for it, then the UI
# ---------------------------------------------------------------------------
port_busy() { "$PY" -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1',$1))==0 else 1)"; }
port_busy "$API_PORT" && die "Port $API_PORT is already in use (set API_PORT=…)"
port_busy "$UI_PORT" && die "Port $UI_PORT is already in use (set UI_PORT=…)"

API_PID=""
UI_PID=""
cleanup() {
  trap - EXIT INT TERM
  log "Stopping…"
  for p in $API_PID $UI_PID; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  exit 0
}
trap cleanup EXIT INT TERM

log "Starting API on http://127.0.0.1:$API_PORT"
"$PY" -m uvicorn backend.app:app --host 127.0.0.1 --port "$API_PORT" &
API_PID=$!

for _ in $(seq 1 60); do
  if "$PY" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$API_PORT/health', timeout=1)" 2>/dev/null; then
    break
  fi
  kill -0 "$API_PID" 2>/dev/null || die "API failed to start (see output above)"
  sleep 1
done

log "Starting UI on http://127.0.0.1:$UI_PORT"
log "The model loads in the background (the first run downloads ~1.2 GB). Ctrl-C stops everything."
SCRUB_API="http://127.0.0.1:$API_PORT" "$PY" -m streamlit run frontend/app.py \
  --server.address 127.0.0.1 --server.port "$UI_PORT" --server.headless true \
  --browser.gatherUsageStats false &
UI_PID=$!

# open the browser on macOS once the UI is up
if [[ "$(uname -s)" == "Darwin" ]]; then
  ( sleep 3; open "http://127.0.0.1:$UI_PORT" ) &
fi

# bash 3.2 (macOS) has no `wait -n`: poll until either process exits
while kill -0 "$API_PID" 2>/dev/null && kill -0 "$UI_PID" 2>/dev/null; do
  sleep 1
done
log "A process exited; shutting down."
