#!/usr/bin/env bash

set -euo pipefail

gitleaks_bin="${GITLEAKS_BIN:-gitleaks}"

if [[ "$gitleaks_bin" == */* ]]; then
  if [[ ! -x "$gitleaks_bin" ]]; then
    echo "Gitleaks binary is not executable: $gitleaks_bin" >&2
    exit 2
  fi
elif ! command -v "$gitleaks_bin" >/dev/null 2>&1; then
  echo "gitleaks is required. Install v8.30.1 before committing." >&2
  exit 2
fi

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$repo_root"

echo "Scanning working tree for secrets..." >&2
"$gitleaks_bin" dir . --config .gitleaks.toml --redact --no-banner

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Scanning Git history for secrets..." >&2
  "$gitleaks_bin" git . --config .gitleaks.toml --redact --no-banner
else
  echo "Git history scan skipped: repository is not initialized yet." >&2
fi
