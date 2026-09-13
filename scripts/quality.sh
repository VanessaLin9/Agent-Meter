#!/usr/bin/env bash
# Local and CI quality entrypoint. Fail-fast; never rewrite uv.lock or auto-fix format.

set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv 0.12.13 is required. Install: https://docs.astral.sh/uv/getting-started/installation/" >&2
  echo "Example: curl -LsSf https://astral.sh/uv/0.12.13/install.sh | sh" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

uv sync --locked --group dev
uv run --no-sync ruff format --check src tests
uv run --no-sync ruff check src tests
uv run --no-sync mypy src tests
uv run --no-sync pytest
