#!/usr/bin/env bash
# The five CI legs, runnable locally as one command (harnessability
# review F1): a commit verified against fewer legs gambles a CI
# round-trip — three happened before this script existed.
set -euo pipefail
cd "$(dirname "$0")/.."
uv run ruff format --check .
uv run ruff check .
uv run bandit -c pyproject.toml -r src -q
uv run pyright
uv run pytest -m "not integration"
