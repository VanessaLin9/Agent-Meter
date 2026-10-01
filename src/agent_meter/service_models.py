"""HTTP-shaped desktop service DTOs without a web framework（PR #8）.

Responsibility: validate health and non-snapshot error envelopes.
Non-goals: FastAPI, bind address, CORS, or calling providers.

Inputs: JSON-like mappings. Outputs: immutable documents or a sanitized
ServiceValidationError. These models are the shared DTO owner for GET /health
and HTTP error bodies; GET /usage success stays in `models.UsageSnapshot`.

Contract: `docs/contracts/desktop-service.md`, `schemas/health-v1.schema.json`,
and `schemas/error-v1.schema.json`.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import ValidationError, model_validator
from pydantic.json_schema import SkipJsonSchema

from agent_meter.models import ContractModel, ErrorMessage

PUBLIC_ERROR_SCHEMA_ID = "https://agent-meter.local/schemas/error-v1.schema.json"
PUBLIC_HEALTH_SCHEMA_ID = "https://agent-meter.local/schemas/health-v1.schema.json"

ServiceErrorCode = Literal[
    "no_enabled_providers",
    "snapshot_not_ready",
    "snapshot_unavailable",
    "config_error",
    "revision_conflict",
    "save_failed",
]
USAGE_UNAVAILABLE_CODES: frozenset[ServiceErrorCode] = frozenset(
    {
        "no_enabled_providers",
        "snapshot_not_ready",
        "snapshot_unavailable",
    }
)
HealthState = Literal["idle", "ready", "degraded"]


class ServiceValidationError(ValueError):
    """Rejected health or error envelope. Message never includes the input."""

    def __init__(self) -> None:
        super().__init__("Rejected malformed service document")


class ServiceError(ContractModel):
    """Whitelist error body. Not an ErrorSummary and not a usage snapshot."""

    code: ServiceErrorCode
    message: ErrorMessage


class ErrorEnvelope(ContractModel):
    """GET /usage 503 body（PR #8）。Never feed this document to UsageSnapshot."""

    error: ServiceError


class HealthDocument(ContractModel):
    """GET /health body（PR #8）。HTTP 200 means the process is up, not provider health."""

    state: HealthState
    error: ServiceError | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def error_matches_state(self) -> Self:
        # CONTRACT: degraded is the only state that may carry an error object.
        if self.state == "degraded":
            if self.error is None:
                raise ValueError("degraded health requires error")
        elif self.error is not None:
            raise ValueError("idle and ready health must omit error")
        return self


def parse_error_envelope(payload: object) -> ErrorEnvelope:
    try:
        return ErrorEnvelope.model_validate(payload)
    except ValidationError:
        raise ServiceValidationError() from None


def dump_error_envelope(envelope: ErrorEnvelope) -> dict[str, Any]:
    return envelope.model_dump(mode="json")


def parse_health_document(payload: object) -> HealthDocument:
    try:
        return HealthDocument.model_validate(payload)
    except ValidationError:
        raise ServiceValidationError() from None


def dump_health_document(document: HealthDocument) -> dict[str, Any]:
    return document.model_dump(mode="json", exclude_unset=True)
