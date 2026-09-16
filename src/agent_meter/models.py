"""Normalized usage snapshot models（PR #2）。

Responsibility: typed, credential-free GET /usage snapshot used by later
cache and API layers. This module validates the v0.1 transport contract.
Non-goals: provider transport, status aggregation, cache I/O, HTTP.

Inputs: JSON-like mappings. Units are UTC Unix seconds and remaining
percentage 0–100. Outputs: immutable UsageSnapshot or ValidationError.

Contract: `docs/contracts/usage-api.md` and
`schemas/usage-v0.1.schema.json`. Retry, cache, and fallback belong to the
orchestrator, not this module.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from pydantic.json_schema import SkipJsonSchema

# CONTRACT: GET /usage consumers and future FastAPI/OpenAPI must use this
# committed schema, not UsageSnapshot.model_json_schema()（PR #2）。
PUBLIC_USAGE_SCHEMA_ID = "https://agent-meter.local/schemas/usage-v0.1.schema.json"


def _coerce_json_integer(value: object) -> object:
    # CONTRACT: Draft 2020-12 integer accepts integer-valued JSON numbers
    # such as 2000000000.0. Normalize to Python int; reject bool and
    # non-integral floats（PR #2）。
    if type(value) is bool:
        raise ValueError("JSON boolean is not an integer")
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


def _drop_json_schema_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


# CONTRACT: generated_at, collected_at, and reset_at are UTC Unix seconds（PR #2）。
UnixSeconds = Annotated[int, BeforeValidator(_coerce_json_integer), Field(ge=0)]
PositiveSeconds = Annotated[int, BeforeValidator(_coerce_json_integer), Field(ge=1)]
# CONTRACT: Percentage is 0–100 inclusive. Fail closed; never clamp to 0 or 100（PR #2）。
Percentage = Annotated[int | float, Field(ge=0, le=100)]
NonNegativeNumber = Annotated[int | float, Field(ge=0)]
PositiveNumber = Annotated[int | float, Field(gt=0)]
ProviderKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
MeterId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
SourceId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=80)]
MeterLabel = Annotated[str, StringConstraints(min_length=1, max_length=40)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
ErrorMessage = Annotated[
    str, StringConstraints(min_length=1, max_length=240, pattern=r"^[^\r\n]+$")
]
ErrorCode = Annotated[
    str, StringConstraints(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
]
OptionalNonNegative = Annotated[
    NonNegativeNumber | SkipJsonSchema[None], Field(json_schema_extra=_drop_json_schema_default)
]
OptionalPositive = Annotated[
    PositiveNumber | SkipJsonSchema[None], Field(json_schema_extra=_drop_json_schema_default)
]
OptionalPercentage = Annotated[
    Percentage | SkipJsonSchema[None], Field(json_schema_extra=_drop_json_schema_default)
]
OptionalCurrency = Annotated[
    CurrencyCode | SkipJsonSchema[None], Field(json_schema_extra=_drop_json_schema_default)
]
OptionalErrorCode = Annotated[
    ErrorCode | SkipJsonSchema[None], Field(json_schema_extra=_drop_json_schema_default)
]

ErrorCategory = Literal[
    "not_configured",
    "not_installed",
    "not_authenticated",
    "auth_expired",
    "timeout",
    "network",
    "upstream",
    "malformed_response",
    "cache",
    "internal",
]


class ContractModel(BaseModel):
    """Shared JSON-contract settings: reject extras, do not coerce strings."""

    # SECURITY: extra="forbid" keeps provider-specific and credential-shaped
    # keys out of the shared transport model（PR #2）。
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _reject_explicit_null(value: object) -> object:
    # CONTRACT: Missing (omit) is not JSON null. Non-nullable optional fields
    # must be omitted rather than set to null（PR #2）。
    if value is None:
        raise ValueError("null is not allowed; omit the field")
    return value


class ErrorSummary(ContractModel):
    """Sanitized failure summary. Never include secrets or raw payloads."""

    category: ErrorCategory
    message: ErrorMessage
    retryable: bool
    code: OptionalErrorCode = None

    @field_validator("code", mode="before")
    @classmethod
    def code_rejects_null(cls, value: object) -> object:
        return _reject_explicit_null(value)


class QuotaMeter(ContractModel):
    """Generic quota meter. remaining_percentage is required; 0 is valid."""

    # CONTRACT: Omitting remaining_percentage is malformed; 0 means zero remaining（PR #2）。

    id: MeterId
    label: MeterLabel
    kind: Literal["quota"]
    unit: Literal["percent", "currency", "requests", "tokens"]
    remaining_percentage: Percentage
    used: OptionalNonNegative = None
    limit: OptionalPositive = None
    remaining: OptionalNonNegative = None
    currency_code: OptionalCurrency = None
    reset_at: UnixSeconds | None = None

    @field_validator("used", "limit", "remaining", "currency_code", mode="before")
    @classmethod
    def optional_numbers_reject_null(cls, value: object) -> object:
        return _reject_explicit_null(value)


class SpendMeter(ContractModel):
    """Generic spend meter. Omit the whole meter when spend is unknown."""

    id: MeterId
    label: MeterLabel
    kind: Literal["spend"]
    unit: Literal["currency"]
    used: NonNegativeNumber
    currency_code: CurrencyCode
    remaining_percentage: OptionalPercentage = None
    limit: OptionalPositive = None
    remaining: OptionalNonNegative = None
    reset_at: UnixSeconds | None = None

    @field_validator("remaining_percentage", "limit", "remaining", mode="before")
    @classmethod
    def optional_numbers_reject_null(cls, value: object) -> object:
        return _reject_explicit_null(value)


Meter = Annotated[QuotaMeter | SpendMeter, Field(discriminator="kind")]


class ProviderOk(ContractModel):
    status: Literal["ok"]
    source: SourceId
    collected_at: UnixSeconds
    stale_after_seconds: PositiveSeconds
    meters: Annotated[list[Meter], Field(min_length=1, max_length=32)]


class ProviderStale(ContractModel):
    status: Literal["stale"]
    source: SourceId
    collected_at: UnixSeconds
    stale_after_seconds: PositiveSeconds
    meters: Annotated[list[Meter], Field(min_length=1, max_length=32)]
    error: ErrorSummary


class ProviderUnavailable(ContractModel):
    status: Literal["unavailable"]
    source: SourceId
    collected_at: Literal[None]
    stale_after_seconds: PositiveSeconds
    meters: Annotated[list[Meter], Field(max_length=0)]
    error: ErrorSummary


class ProviderError(ContractModel):
    status: Literal["error"]
    source: SourceId
    collected_at: Literal[None]
    stale_after_seconds: PositiveSeconds
    meters: Annotated[list[Meter], Field(max_length=0)]
    error: ErrorSummary


ProviderSnapshot = Annotated[
    ProviderOk | ProviderStale | ProviderUnavailable | ProviderError,
    Field(discriminator="status"),
]


class UsageSnapshot(ContractModel):
    """Top-level normalized snapshot. Aggregation is applied by callers."""

    schema_version: Literal["0.1"]
    generated_at: UnixSeconds
    status: Literal["ok", "partial", "error"]
    providers: Annotated[dict[ProviderKey, ProviderSnapshot], Field(min_length=1)]


def parse_usage_snapshot(payload: object) -> UsageSnapshot:
    """Parse a snapshot mapping. Fail closed on malformed data."""

    # FALLBACK: Validation errors are raised as-is. Callers must not clamp
    # percentages, invent zeros, or continue with a partial snapshot（PR #2）。
    return UsageSnapshot.model_validate(payload)


def dump_usage_snapshot(snapshot: UsageSnapshot) -> dict[str, Any]:
    """Dump a snapshot to JSON-ready dict, omitting unset optional fields."""

    return snapshot.model_dump(mode="json", exclude_unset=True)
