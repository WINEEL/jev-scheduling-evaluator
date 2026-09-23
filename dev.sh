#!/usr/bin/env bash

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

if [ ! -x "$ROOT/backend/.venv/bin/python" ]; then
  echo "Backend virtual environment not found."
  echo "Run:"
  echo "  cd backend"
  echo "  python3 -m venv .venv"
  echo '  source .venv/bin/activate'
  echo '  pip install -e ".[dev]"'
  exit 1
fi

if [ ! -f "$ROOT/.env" ]; then
  echo ".env not found."
  echo "Create it from .env.example and add TYPESAFE_API_KEY."
  exit 1
fi

set -a
source "$ROOT/.env"
set +a

cleanup() {
  echo
  echo "Stopping Jev demo..."
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

echo "Starting Jev demo..."
echo "Backend: http://127.0.0.1:8000"
echo "Frontend: http://localhost:3000"
echo

(
  cd "$ROOT/backend"
  exec "$ROOT/backend/.venv/bin/python" -m uvicorn app.main:app \
    --reload \
    --port 8000
) &
BACKEND_PID=$!

(
  cd "$ROOT/frontend"
  exec npm run dev
) &
FRONTEND_PID=$!

wait
