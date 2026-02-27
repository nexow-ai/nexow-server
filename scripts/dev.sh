#!/usr/bin/env bash
# Start API + worker + sandbox. Ctrl+C stops all.
set -e
trap 'kill -TERM 0 2>/dev/null || true; exit 130' INT TERM
trap 'kill -TERM 0 2>/dev/null || true' EXIT

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

uv run uvicorn nexow.api.app:app --host 0.0.0.0 --port 8000 --reload &
uv run python -m nexow.worker &
uv run python -m nexow.snapshot &
(cd sandbox && pnpm run dev) &
wait
