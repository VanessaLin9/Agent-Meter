"""JSON Schema 與 typed models 雙向 contract tests。

Valid fixtures 必須被 Draft 2020-12 schema 與 Pydantic 同時接受並 round-trip。
Invalid fixtures 必須被兩邊以 validation error 拒絕。兩邊判斷不一致時失敗。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from jsonschema.validators import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError

from agent_meter.models import (
    SpendMeter,
    UsageSnapshot,
    dump_usage_snapshot,
    parse_usage_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "usage-v0.1.schema.json"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "contracts"
INVALID_DIR = FIXTURE_DIR / "invalid"


def _load_mapping(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), path.name
    return payload


def _valid_fixture_paths() -> list[Path]:
    return sorted(path for path in FIXTURE_DIR.glob("*.json") if path.is_file())


def _invalid_fixture_paths() -> list[Path]:
    return sorted(path for path in INVALID_DIR.glob("*.json") if path.is_file())


def _schema_validator() -> Draft202012Validator:
    schema = _load_mapping(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _schema_accepts(validator: Draft202012Validator, payload: dict[str, Any]) -> bool:
    return bool(validator.is_valid(payload))


def _model_accepts(payload: dict[str, Any]) -> bool:
    try:
        parse_usage_snapshot(payload)
    except PydanticValidationError:
        return False
    return True


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    return _schema_validator()


@pytest.mark.parametrize("path", _valid_fixture_paths(), ids=lambda path: path.name)
def test_valid_fixtures_parse_round_trip_and_match_schema(
    path: Path, schema_validator: Draft202012Validator
) -> None:
    payload = _load_mapping(path)
    schema_validator.validate(payload)

    snapshot = parse_usage_snapshot(payload)
    dumped = dump_usage_snapshot(snapshot)
    assert dumped == payload

    schema_validator.validate(dumped)
    assert parse_usage_snapshot(dumped) == snapshot


@pytest.mark.parametrize("path", _invalid_fixture_paths(), ids=lambda path: path.name)
def test_invalid_fixtures_rejected_by_schema_and_model(
    path: Path, schema_validator: Draft202012Validator
) -> None:
    payload = _load_mapping(path)

    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(payload)
    with pytest.raises(PydanticValidationError):
        parse_usage_snapshot(payload)


@pytest.mark.parametrize(
    "path",
    _valid_fixture_paths() + _invalid_fixture_paths(),
    ids=lambda path: path.parent.name + "/" + path.name,
)
def test_schema_and_model_accept_or_reject_together(
    path: Path, schema_validator: Draft202012Validator
) -> None:
    payload = _load_mapping(path)
    assert _schema_accepts(schema_validator, payload) == _model_accepts(payload)


def test_cursor_two_pools_and_optional_spend_are_generic_meters() -> None:
    snapshot = parse_usage_snapshot(_load_mapping(FIXTURE_DIR / "ok-with-spend.json"))
    cursor = snapshot.providers["cursor"]
    meter_ids = [meter.id for meter in cursor.meters]
    assert meter_ids == ["cursor_models", "other_models", "on_demand"]
    assert all(meter.kind == "quota" for meter in cursor.meters[:2])
    spend = cursor.meters[2]
    assert isinstance(spend, SpendMeter)
    assert spend.unit == "currency"
    assert spend.used == 4.5
    assert spend.currency_code == "USD"


def test_zero_remaining_is_not_treated_as_missing(
    schema_validator: Draft202012Validator,
) -> None:
    payload = _load_mapping(FIXTURE_DIR / "ok.json")
    meter = payload["providers"]["codex"]["meters"][0]
    meter["remaining_percentage"] = 0
    schema_validator.validate(payload)
    snapshot = parse_usage_snapshot(payload)
    assert snapshot.providers["codex"].meters[0].remaining_percentage == 0


def test_missing_remaining_percentage_is_rejected(
    schema_validator: Draft202012Validator,
) -> None:
    payload = _load_mapping(FIXTURE_DIR / "ok.json")
    del payload["providers"]["codex"]["meters"][0]["remaining_percentage"]
    assert not _schema_accepts(schema_validator, payload)
    assert not _model_accepts(payload)


def test_percentage_over_100_is_not_clamped(
    schema_validator: Draft202012Validator,
) -> None:
    payload = _load_mapping(FIXTURE_DIR / "ok.json")
    payload["providers"]["codex"]["meters"][0]["remaining_percentage"] = 101
    with pytest.raises(JsonSchemaValidationError):
        schema_validator.validate(payload)
    with pytest.raises(PydanticValidationError):
        parse_usage_snapshot(payload)


def test_unknown_provider_status_is_rejected(
    schema_validator: Draft202012Validator,
) -> None:
    payload = _load_mapping(FIXTURE_DIR / "ok.json")
    payload["providers"]["codex"]["status"] = "degraded"
    assert not _schema_accepts(schema_validator, payload)
    assert not _model_accepts(payload)


def test_provider_specific_upstream_field_is_rejected(
    schema_validator: Draft202012Validator,
) -> None:
    payload = _load_mapping(FIXTURE_DIR / "ok.json")
    payload["providers"]["cursor"]["autoPercentUsed"] = 63.45
    assert not _schema_accepts(schema_validator, payload)
    assert not _model_accepts(payload)


def test_invalid_fixtures_directory_is_not_empty() -> None:
    assert _invalid_fixture_paths()


def test_usage_schema_is_draft_2020_12() -> None:
    schema = _load_mapping(SCHEMA_PATH)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert UsageSnapshot.model_fields["schema_version"].annotation is not None
