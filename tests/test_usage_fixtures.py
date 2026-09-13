"""Smoke-check committed usage fixtures without JSON Schema validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "contracts"
REQUIRED_FIXTURES = ("ok.json", "partial.json", "error.json")
REQUIRED_TOP_LEVEL_KEYS = ("schema_version", "generated_at", "status", "providers")


def test_committed_usage_fixtures_exist_and_have_top_level_keys() -> None:
    paths = [FIXTURE_DIR / name for name in REQUIRED_FIXTURES]
    missing = [path.name for path in paths if not path.is_file()]
    assert missing == []

    snapshots: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict), path.name
        missing_keys = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in payload]
        assert missing_keys == [], f"{path.name} missing {missing_keys}"
        snapshots.append(payload)

    assert snapshots
