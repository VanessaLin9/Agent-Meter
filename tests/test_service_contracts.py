"""Settings document, write-request, health, and error-envelope contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from jsonschema.validators import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError

from agent_meter.freshness import DEFAULT_STALE_AFTER_SECONDS
from agent_meter.models import UsageSnapshot, parse_usage_snapshot
from agent_meter.providers.claude import CLAUDE_PROVIDER_ID
from agent_meter.providers.codex_rpc import CODEX_PROVIDER_ID
from agent_meter.providers.cursor_rpc import CURSOR_PROVIDER_ID
from agent_meter.service_models import (
    PUBLIC_ERROR_SCHEMA_ID,
    PUBLIC_HEALTH_SCHEMA_ID,
    ServiceValidationError,
    dump_error_envelope,
    dump_health_document,
    parse_error_envelope,
    parse_health_document,
)
from agent_meter.service_policy import (
    EVENT_DRIVEN_PROVIDER_IDS,
    POLL_INTERVAL_SECONDS,
    POLL_TIMEOUT_SECONDS,
    POLLED_PROVIDER_IDS,
    RETRY_BACKOFF_CAP_SECONDS,
    STALE_AFTER_SECONDS,
    enabled_provider_ids,
    health_document,
    is_complete_enabled_snapshot,
    usage_error,
)
from agent_meter.settings import (
    ENABLED_PROVIDER_IDS,
    PUBLIC_SETTINGS_SCHEMA_ID,
    PUBLIC_SETTINGS_WRITE_SCHEMA_ID,
    SettingsValidationError,
    dump_settings,
    dump_settings_write,
    first_install_settings,
    parse_settings,
    parse_settings_write,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_SCHEMA = REPO_ROOT / "schemas" / "settings-v1.schema.json"
SETTINGS_WRITE_SCHEMA = REPO_ROOT / "schemas" / "settings-write-v1.schema.json"
ERROR_SCHEMA = REPO_ROOT / "schemas" / "error-v1.schema.json"
HEALTH_SCHEMA = REPO_ROOT / "schemas" / "health-v1.schema.json"
SETTINGS_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "settings"
ERROR_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "service" / "errors"
HEALTH_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "service" / "health"
USAGE_OK = Path(__file__).resolve().parent / "fixtures" / "contracts" / "ok.json"
FAKE_SECRET = "PLANTED_PROVIDER_TOKEN_VALUE_DO_NOT_EMIT"


def _load_mapping(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), path.name
    return payload


def _json_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _schema_validator(path: Path) -> Draft202012Validator:
    schema = _load_mapping(path)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_enabled_provider_ids_match_adapter_ids() -> None:
    assert ENABLED_PROVIDER_IDS == ("codex", "cursor", "claude")
    assert set(ENABLED_PROVIDER_IDS) == {
        CODEX_PROVIDER_ID,
        CURSOR_PROVIDER_ID,
        CLAUDE_PROVIDER_ID,
    }


def test_public_schema_ids_match_committed_files() -> None:
    assert PUBLIC_SETTINGS_SCHEMA_ID.endswith("settings-v1.schema.json")
    assert PUBLIC_SETTINGS_WRITE_SCHEMA_ID.endswith("settings-write-v1.schema.json")
    assert PUBLIC_ERROR_SCHEMA_ID.endswith("error-v1.schema.json")
    assert PUBLIC_HEALTH_SCHEMA_ID.endswith("health-v1.schema.json")


@pytest.mark.parametrize(
    "path",
    [path for path in _json_files(SETTINGS_FIXTURES) if path.name != "write-request.json"],
    ids=lambda path: path.name,
)
def test_valid_settings_fixtures_round_trip(path: Path) -> None:
    payload = _load_mapping(path)
    validator = _schema_validator(SETTINGS_SCHEMA)
    validator.validate(payload)
    parsed = parse_settings(payload)
    assert dump_settings(parsed) == payload


def test_valid_settings_write_fixture_round_trip() -> None:
    payload = _load_mapping(SETTINGS_FIXTURES / "write-request.json")
    validator = _schema_validator(SETTINGS_WRITE_SCHEMA)
    validator.validate(payload)
    parsed = parse_settings_write(payload)
    assert dump_settings_write(parsed) == payload


@pytest.mark.parametrize(
    "path", _json_files(SETTINGS_FIXTURES / "invalid"), ids=lambda path: path.name
)
def test_invalid_settings_fixtures_are_rejected(path: Path) -> None:
    payload = _load_mapping(path)
    validator = (
        _schema_validator(SETTINGS_WRITE_SCHEMA)
        if path.name.startswith("write-")
        else _schema_validator(SETTINGS_SCHEMA)
    )
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(payload)
    with pytest.raises(SettingsValidationError) as exc_info:
        if path.name.startswith("write-"):
            parse_settings_write(payload)
        else:
            parse_settings(payload)
    rendered = str(exc_info.value)
    assert FAKE_SECRET not in rendered
    assert exc_info.value.__cause__ is None


def test_unknown_token_field_is_not_echoed() -> None:
    payload = _load_mapping(SETTINGS_FIXTURES / "invalid" / "unknown-token-field.json")
    assert FAKE_SECRET in json.dumps(payload)
    with pytest.raises(SettingsValidationError) as exc_info:
        parse_settings(payload)
    assert FAKE_SECRET not in str(exc_info.value)
    assert FAKE_SECRET not in repr(exc_info.value)


def test_first_install_is_empty_enablement() -> None:
    settings = first_install_settings()
    assert settings.settings_version == 1
    assert settings.revision == 0
    assert settings.enabled_providers == []
    assert enabled_provider_ids(settings) == ()


@pytest.mark.parametrize("path", _json_files(ERROR_FIXTURES), ids=lambda path: path.name)
def test_valid_error_fixtures_round_trip(path: Path) -> None:
    payload = _load_mapping(path)
    validator = _schema_validator(ERROR_SCHEMA)
    validator.validate(payload)
    parsed = parse_error_envelope(payload)
    assert dump_error_envelope(parsed) == payload
    with pytest.raises(PydanticValidationError):
        parse_usage_snapshot(payload)


@pytest.mark.parametrize(
    "path", _json_files(ERROR_FIXTURES / "invalid"), ids=lambda path: path.name
)
def test_invalid_error_fixtures_are_rejected(path: Path) -> None:
    payload = _load_mapping(path)
    with pytest.raises(JsonSchemaValidationError):
        _schema_validator(ERROR_SCHEMA).validate(payload)
    with pytest.raises(ServiceValidationError) as exc_info:
        parse_error_envelope(payload)
    assert FAKE_SECRET not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize("path", _json_files(HEALTH_FIXTURES), ids=lambda path: path.name)
def test_valid_health_fixtures_round_trip(path: Path) -> None:
    payload = _load_mapping(path)
    validator = _schema_validator(HEALTH_SCHEMA)
    validator.validate(payload)
    parsed = parse_health_document(payload)
    assert dump_health_document(parsed) == payload


@pytest.mark.parametrize(
    "path", _json_files(HEALTH_FIXTURES / "invalid"), ids=lambda path: path.name
)
def test_invalid_health_fixtures_are_rejected(path: Path) -> None:
    payload = _load_mapping(path)
    with pytest.raises(JsonSchemaValidationError):
        _schema_validator(HEALTH_SCHEMA).validate(payload)
    with pytest.raises(ServiceValidationError):
        parse_health_document(payload)


def test_usage_error_policy_covers_three_unavailable_codes() -> None:
    empty = first_install_settings()
    empty_error = usage_error(settings=empty, snapshot=None)
    assert empty_error is not None
    assert empty_error.code == "no_enabled_providers"

    enabled = parse_settings(_load_mapping(SETTINGS_FIXTURES / "single-provider.json"))
    pending = usage_error(settings=enabled, snapshot=None)
    assert pending is not None
    assert pending.code == "snapshot_not_ready"
    config_damage = usage_error(settings=None, snapshot=None)
    assert config_damage is not None
    assert config_damage.code == "snapshot_unavailable"
    internal = usage_error(settings=enabled, snapshot=None, internal_failure=True)
    assert internal is not None
    assert internal.code == "snapshot_unavailable"

    snapshot = parse_usage_snapshot(_load_mapping(USAGE_OK))
    incomplete = usage_error(settings=enabled, snapshot=snapshot)
    assert incomplete is not None
    assert incomplete.code == "snapshot_not_ready"
    complete = UsageSnapshot(
        schema_version="0.1",
        generated_at=snapshot.generated_at,
        status="ok",
        providers={"codex": snapshot.providers["codex"]},
    )
    assert is_complete_enabled_snapshot(complete, enabled.enabled_providers)
    assert usage_error(settings=enabled, snapshot=complete) is None


def test_health_policy_does_not_claim_provider_health() -> None:
    assert dump_health_document(health_document(settings=first_install_settings())) == {
        "state": "idle"
    }
    enabled = parse_settings(_load_mapping(SETTINGS_FIXTURES / "single-provider.json"))
    assert dump_health_document(health_document(settings=enabled)) == {"state": "ready"}
    degraded = health_document(settings=None)
    assert degraded.state == "degraded"
    assert degraded.error is not None
    assert degraded.error.code == "config_error"


def test_polling_constants_match_desktop_contract() -> None:
    assert POLL_INTERVAL_SECONDS == 300
    assert POLL_TIMEOUT_SECONDS == 20
    assert RETRY_BACKOFF_CAP_SECONDS == 900
    assert STALE_AFTER_SECONDS == DEFAULT_STALE_AFTER_SECONDS == 900
    assert POLLED_PROVIDER_IDS == ("codex", "cursor")
    assert EVENT_DRIVEN_PROVIDER_IDS == ("claude",)
