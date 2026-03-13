#!/usr/bin/env bash
# Scripted test: pytest + integration smoke. Run from repo root.
# Requires: uv sync --extra test (or pip install pytest httpx)
set -e
cd "$(dirname "$0")/.."
run_pytest() {
  if command -v uv >/dev/null 2>&1; then
    uv run pytest tests/ -v --tb=short
  else
    python3 -m pytest tests/ -v --tb=short
  fi
}
run_pytest
if command -v uv >/dev/null 2>&1; then
  uv run python scripts/integration_smoke.py
else
  python3 scripts/integration_smoke.py
fi
