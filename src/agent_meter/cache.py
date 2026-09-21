"""Last-valid snapshot cache envelope and last-good merge policy（PR #3）。

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

import re
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

# SECURITY: 不得把 ValidationError 或 raw payload 字串化進 last_error；
# pydantic 錯誤可能回顯被拒絕的 input，包含 planted secret（PR #3）。
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
_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_MALFORMED_SOURCE = "malformed_result"

# CONTRACT: Adapter failure 不直接決定 stale／error。無 last-good 且
# provider 不可用 → unavailable；無 last-good 且執行失敗 → error；
# 任何失敗只要有 last-good → stale（PR #3）。
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

    # SECURITY: extra="forbid"；envelope 不得新增 raw_response、headers
    # 或 credential 欄位（PR #3）。
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
        # FALLBACK: malformed payload 不覆蓋 last-valid，也不發明 meters（PR #3）。
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

    try:
        return _apply_collection_result(previous, result, settings=settings)
    except ValidationError:
        # FALLBACK: ProviderOk／ErrorSummary 建構失敗不得逃出單一 provider，
        # 否則 merge 會中斷、其他 provider 也無法套用 last-good（PR #3）。
        return _malformed_provider_snapshot(previous, result, settings=settings)


def _apply_collection_result(
    previous: ProviderSnapshot | None,
    result: ProviderCollectionSuccess | ProviderCollectionFailure,
    *,
    settings: StaleSettings,
) -> ProviderSnapshot:

    if isinstance(result, ProviderCollectionSuccess) and result.meters:
        return ProviderOk.model_validate(
            {
                "status": "ok",
                "source": result.source,
                "collected_at": result.collected_at,
                "stale_after_seconds": settings.for_provider(result.provider_id),
                "meters": [_meter_payload(meter) for meter in result.meters],
            }
        )

    error = _error_from_result(result)
    last_good = _last_good(previous)
    if last_good is not None:
        source, collected_at, stale_after_seconds, meters = last_good
        # FALLBACK: 保留原 meters 與 collected_at；不得用空 meter 覆蓋 last-good（PR #3）。
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


def _malformed_provider_snapshot(
    previous: ProviderSnapshot | None,
    result: ProviderCollectionSuccess | ProviderCollectionFailure,
    *,
    settings: StaleSettings,
) -> ProviderSnapshot:
    last_good = _last_good(previous)
    if last_good is not None:
        source, collected_at, stale_after_seconds, meters = last_good
        return ProviderStale(
            status="stale",
            source=source,
            collected_at=collected_at,
            stale_after_seconds=stale_after_seconds,
            meters=meters,
            error=MALFORMED_PROVIDER_ERROR,
        )
    return ProviderError(
        status="error",
        source=_safe_source(result.source),
        collected_at=None,
        stale_after_seconds=settings.for_provider(result.provider_id),
        meters=[],
        error=MALFORMED_PROVIDER_ERROR,
    )


def _safe_source(source: str) -> str:
    if isinstance(source, str) and _SOURCE_ID_PATTERN.fullmatch(source):
        return source
    return _MALFORMED_SOURCE


def _meter_payload(meter: Meter) -> object:
    # CONTRACT: 已建構的 Meter 實例（含 model_construct）必須再走 schema
    # validation，否則 remaining_percentage=101 會直接進 ProviderOk（PR #3）。
    if isinstance(meter, ContractModel):
        return meter.model_dump(mode="python", exclude_none=True)
    return meter
