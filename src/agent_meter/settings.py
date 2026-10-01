"""Typed provider-enablement settings（desktop service contract）.

Responsibility: validate the persisted settings document and PUT body.
Non-goals: filesystem I/O, HTTP, scheduler, or reading provider credentials.

Inputs: JSON-like mappings. Outputs: immutable Settings / SettingsWriteRequest,
or a sanitized SettingsValidationError. Units: settings_version is the
document schema; revision is a non-negative CAS integer.

Contract: `docs/contracts/settings.md` and `schemas/settings-v1.schema.json`.
The store owns retry/atomic replace; this module never touches disk.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, ValidationError, field_validator

from agent_meter.models import ContractModel

PUBLIC_SETTINGS_SCHEMA_ID = "https://agent-meter.local/schemas/settings-v1.schema.json"
PUBLIC_SETTINGS_WRITE_SCHEMA_ID = "https://agent-meter.local/schemas/settings-write-v1.schema.json"

EnabledProvider = Literal["codex", "cursor", "claude"]
ENABLED_PROVIDER_IDS: tuple[EnabledProvider, ...] = ("codex", "cursor", "claude")
SETTINGS_VERSION: Literal[1] = 1


def _coerce_json_integer(value: object) -> object:
    # CONTRACT: Draft 2020-12 integer accepts integer-valued JSON numbers.
    # Reject bool and non-integral floats; never clamp negatives to 0.
    if type(value) is bool:
        raise ValueError("JSON boolean is not an integer")
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


NonNegativeInt = Annotated[int, BeforeValidator(_coerce_json_integer), Field(ge=0)]
EnabledProviderList = Annotated[list[EnabledProvider], Field(max_length=3)]


class SettingsValidationError(ValueError):
    """Rejected settings payload. Message never includes the input document."""

    # SECURITY: pydantic ValidationError can echo planted secrets; callers
    # must raise this wrapper and never stringify the cause.
    code = "config_error"

    def __init__(self) -> None:
        super().__init__("Rejected malformed settings")


class Settings(ContractModel):
    """Persisted settings document and GET /settings body."""

    # SECURITY: extra="forbid" rejects token/path/account fields. There is no
    # secret-bearing settings key in v1.
    settings_version: Literal[1]
    revision: NonNegativeInt
    enabled_providers: EnabledProviderList

    @field_validator("enabled_providers")
    @classmethod
    def enabled_providers_are_unique(cls, value: list[EnabledProvider]) -> list[EnabledProvider]:
        # CONTRACT: duplicates are a client error, not silently uniquified.
        if len(value) != len(set(value)):
            raise ValueError("enabled_providers must not contain duplicates")
        return value


class SettingsWriteRequest(ContractModel):
    """PUT /settings body. Does not carry settings_version or the new revision."""

    expected_revision: NonNegativeInt
    enabled_providers: EnabledProviderList

    @field_validator("enabled_providers")
    @classmethod
    def enabled_providers_are_unique(cls, value: list[EnabledProvider]) -> list[EnabledProvider]:
        if len(value) != len(set(value)):
            raise ValueError("enabled_providers must not contain duplicates")
        return value


def first_install_settings() -> Settings:
    """Missing settings file is an empty enablement list, not all-providers-on."""

    # CONTRACT: new desktop service starts with enabled_providers=[]. One-shot
    # CLI --live is a separate entrypoint and keeps collecting all adapters.
    return Settings(settings_version=SETTINGS_VERSION, revision=0, enabled_providers=[])


def parse_settings(payload: object) -> Settings:
    """Parse a settings mapping. Fail closed on unknown fields or types."""

    try:
        return Settings.model_validate(payload)
    except ValidationError:
        raise SettingsValidationError() from None


def dump_settings(settings: Settings) -> dict[str, Any]:
    """Dump settings to a JSON-ready dict with every required field."""

    return settings.model_dump(mode="json")


def parse_settings_write(payload: object) -> SettingsWriteRequest:
    """Parse a PUT /settings mapping. Fail closed on unknown fields or types."""

    try:
        return SettingsWriteRequest.model_validate(payload)
    except ValidationError:
        raise SettingsValidationError() from None


def dump_settings_write(request: SettingsWriteRequest) -> dict[str, Any]:
    return request.model_dump(mode="json")


def next_settings(current: Settings, request: SettingsWriteRequest) -> Settings:
    """Build the document that a successful CAS commit would persist."""

    # CONTRACT: revision is the persistence CAS token. HTTP 409 mapping belongs
    # to the API layer; this helper only produces revision+1.
    if request.expected_revision != current.revision:
        raise SettingsRevisionMismatch()
    return Settings(
        settings_version=SETTINGS_VERSION,
        revision=current.revision + 1,
        enabled_providers=list(request.enabled_providers),
    )


class SettingsRevisionMismatch(ValueError):
    """CAS expected_revision does not match the loaded document."""

    code = "revision_conflict"

    def __init__(self) -> None:
        super().__init__("Settings revision does not match")
