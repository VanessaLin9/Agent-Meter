#!/usr/bin/env bash
# PR #1: 本機與 CI 同一條 quality 入口。用 --locked 而非 --frozen，因為 --frozen 會略過 lock freshness。
# 失敗即停；不改 lock、不自動修格式、不自動安裝 uv。

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
