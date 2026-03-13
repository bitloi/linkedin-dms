#!/usr/bin/env bash
# Scripted test: run pytest for sync/send orchestration and API.
# Requires: uv sync --extra dev (or pip install pytest) then run from repo root.
set -e
cd "$(dirname "$0")/.."
if command -v uv >/dev/null 2>&1; then
  uv run pytest tests/ -v --tb=short
else
  python3 -m pytest tests/ -v --tb=short
fi
