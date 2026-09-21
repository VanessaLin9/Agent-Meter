"""Last-valid snapshot cache envelope and last-good merge policy.

Responsibility: define the in-memory cache envelope and how new collection
results replace or keep last-good provider data. Non-goals: atomic file write,
scheduler, HTTP, or reading credentials.

Inputs: previous envelope/snapshot plus a typed collection result or a JSON-like
payload. Outputs: a new envelope or provider snapshot. Units stay UTC Unix
seconds.

Contract: `docs/contracts/usage-api.md` fallback rules. Orchestrator owns I/O
and retry; this module never touches the filesystem.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from agent_meter.aggregation import build_snapshot
from agent_meter.freshness import StaleSettings
from agent_meter.models import (
    ContractModel,
    ErrorCategory,
    ErrorSummary,
    Meter,
    ProviderError,
    ProviderOk,
    ProviderSnapshot,
    ProviderStale,
    ProviderUnavailable,
    UsageSnapshot,
    parse_usage_snapshot,
)

# SECURITY: Never stringify ValidationError or raw payload into last_error;
# pydantic errors can echo rejected input, including planted secrets.
MALFORMED_SNAPSHOT_ERROR = ErrorSummary(
    category="malformed_response",
    message="Rejected malformed snapshot",
    retryable=False,
)
MALFORMED_PROVIDER_ERROR = ErrorSummary(
    category="malformed_response",
    message="Rejected malformed provider result",
    retryable=False,
)

# CONTRACT: Adapter failure categories do not choose stale vs error. No
# last-good plus an unusable provider → unavailable; no last-good plus an
# execution failure → error; any failure with last-good → stale.
_UNAVAILABLE_WITHOUT_LAST_GOOD: frozenset[ErrorCategory] = frozenset(
    {
        "not_configured",
        "not_installed",
        "not_authenticated",
        "auth_expired",
    }
)


class CacheEnvelope(ContractModel):
    """Last schema-valid snapshot plus sanitized apply metadata. No I/O."""

    # SECURITY: extra="forbid" on ContractModel; never add raw_response,
    # headers, or credential fields to this envelope.
    snapshot: UsageSnapshot | None = None
    last_error: ErrorSummary | None = None


@dataclass(frozen=True)
class ProviderCollectionSuccess:
    """Typed adapter success. Threshold comes from StaleSettings, not the adapter."""

    provider_id: str
    source: str
    collected_at: int
    meters: tuple[Meter, ...]


@dataclass(frozen=True)
class ProviderCollectionFailure:
    """Typed adapter failure. Sanitized before it reaches this module."""

    provider_id: str
    source: str
    category: ErrorCategory
    message: str
    retryable: bool
    code: str | None = None


def apply_snapshot_payload(envelope: CacheEnvelope, payload: object) -> CacheEnvelope:
    """Store a valid snapshot, or keep last-valid and record a sanitized error."""

    try:
        snapshot = parse_usage_snapshot(payload)
    except ValidationError:
        # FALLBACK: malformed payload never overwrites last-valid data and never
        # invents meters.
        return CacheEnvelope(snapshot=envelope.snapshot, last_error=MALFORMED_SNAPSHOT_ERROR)
    return CacheEnvelope(snapshot=snapshot, last_error=None)


def evaluate_cached_snapshot(envelope: CacheEnvelope, *, now: int) -> UsageSnapshot | None:
    """Recompute stale after restore. Missing cache stays missing."""

    if envelope.snapshot is None:
        return None
    return build_snapshot(envelope.snapshot.providers, now=now)


def apply_collection_result(
    previous: ProviderSnapshot | None,
    result: ProviderCollectionSuccess | ProviderCollectionFailure,
    *,
    settings: StaleSettings,
) -> ProviderSnapshot:
    """Merge one provider result with last-good data. Other providers are untouched."""

    if isinstance(result, ProviderCollectionSuccess) and result.meters:
        return ProviderOk(
            status="ok",
            source=result.source,
            collected_at=result.collected_at,
            stale_after_seconds=settings.for_provider(result.provider_id),
            meters=list(result.meters),
        )

    error = _error_from_result(result)
    last_good = _last_good(previous)
    if last_good is not None:
        source, collected_at, stale_after_seconds, meters = last_good
        # FALLBACK: keep original meters and collected_at; never cover last-good
        # with an empty meter list.
        return ProviderStale(
            status="stale",
            source=source,
            collected_at=collected_at,
            stale_after_seconds=stale_after_seconds,
            meters=meters,
            error=error,
        )

    stale_after_seconds = settings.for_provider(result.provider_id)
    if error.category in _UNAVAILABLE_WITHOUT_LAST_GOOD:
        return ProviderUnavailable(
            status="unavailable",
            source=result.source,
            collected_at=None,
            stale_after_seconds=stale_after_seconds,
            meters=[],
            error=error,
        )
    return ProviderError(
        status="error",
        source=result.source,
        collected_at=None,
        stale_after_seconds=stale_after_seconds,
        meters=[],
        error=error,
    )


def merge_collection_results(
    previous: Mapping[str, ProviderSnapshot],
    results: Sequence[ProviderCollectionSuccess | ProviderCollectionFailure],
    *,
    settings: StaleSettings,
) -> dict[str, ProviderSnapshot]:
    """Apply each result independently so one failure cannot rewrite another provider."""

    merged = dict(previous)
    for result in results:
        merged[result.provider_id] = apply_collection_result(
            merged.get(result.provider_id),
            result,
            settings=settings,
        )
    return merged


def _error_from_result(
    result: ProviderCollectionSuccess | ProviderCollectionFailure,
) -> ErrorSummary:
    if isinstance(result, ProviderCollectionFailure):
        # CONTRACT: omit optional `code`; explicit JSON null is not allowed.
        if result.code is None:
            return ErrorSummary(
                category=result.category,
                message=result.message,
                retryable=result.retryable,
            )
        return ErrorSummary(
            category=result.category,
            message=result.message,
            retryable=result.retryable,
            code=result.code,
        )
    return MALFORMED_PROVIDER_ERROR


def _last_good(
    snapshot: ProviderSnapshot | None,
) -> tuple[str, int, int, list[Meter]] | None:
    if isinstance(snapshot, (ProviderOk, ProviderStale)):
        return (
            snapshot.source,
            snapshot.collected_at,
            snapshot.stale_after_seconds,
            list(snapshot.meters),
        )
    return None
