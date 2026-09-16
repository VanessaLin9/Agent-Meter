"""對已 commit 的 usage fixtures 做頂層欄位 smoke（PR #1）。

完整 Draft 2020-12 與 Pydantic 雙向驗證見 `test_usage_models.py`（PR #2）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "contracts"
REQUIRED_TOP_LEVEL_KEYS = ("schema_version", "generated_at", "status", "providers")


def _valid_fixture_paths() -> list[Path]:
    return sorted(path for path in FIXTURE_DIR.glob("*.json") if path.is_file())


def test_committed_usage_fixtures_exist_and_have_top_level_keys() -> None:
    paths = _valid_fixture_paths()
    names = {path.name for path in paths}
    assert {"ok.json", "partial.json", "error.json", "ok-with-spend.json"} <= names

    snapshots: list[dict[str, Any]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict), path.name
        missing_keys = [key for key in REQUIRED_TOP_LEVEL_KEYS if key not in payload]
        assert missing_keys == [], f"{path.name} missing {missing_keys}"
        snapshots.append(payload)

    assert snapshots
