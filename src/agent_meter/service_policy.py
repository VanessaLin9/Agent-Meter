"""Desktop-service enablement and HTTP decision helpers.

Responsibility: decide which providers belong in a snapshot, which GET /usage
503 applies, and which GET /health state applies. Non-goals: filesystem,
FastAPI, scheduling, or adapter I/O.

Inputs: a loaded Settings document (or None when config is unreadable) plus
an optional schema-valid snapshot. Outputs: ServiceError / HealthDocument.
Units: poll interval and timeouts are seconds.

Contract: `docs/contracts/desktop-service.md`. The API layer maps these
documents to HTTP; this module does not import a web framework.
"""

from __future__ import annotations

from collections.abc import Sequence

from agent_meter.freshness import DEFAULT_STALE_AFTER_SECONDS
from agent_meter.models import UsageSnapshot
from agent_meter.service_models import ErrorEnvelope, HealthDocument, ServiceError
from agent_meter.settings import EnabledProvider, Settings

# CONTRACT: Codex/Cursor poll cadence for the desktop service. Claude is
# event-driven and must not use this poller. Adapter transport may use a
# tighter deadline; B2-03 owns the scheduler.
POLL_INTERVAL_SECONDS = 300
POLL_TIMEOUT_SECONDS = 20
RETRY_BACKOFF_CAP_SECONDS = 900
STALE_AFTER_SECONDS = DEFAULT_STALE_AFTER_SECONDS
POLLED_PROVIDER_IDS: tuple[EnabledProvider, ...] = ("codex", "cursor")
EVENT_DRIVEN_PROVIDER_IDS: tuple[EnabledProvider, ...] = ("claude",)

NO_ENABLED_PROVIDERS = ServiceError(
    code="no_enabled_providers",
    message="No providers enabled",
)
SNAPSHOT_NOT_READY = ServiceError(
    code="snapshot_not_ready",
    message="Usage snapshot is not ready",
)
SNAPSHOT_UNAVAILABLE = ServiceError(
    code="snapshot_unavailable",
    message="Usage snapshot is unavailable",
)
CONFIG_ERROR = ServiceError(
    code="config_error",
    message="Settings could not be read",
)


def enabled_provider_ids(settings: Settings) -> tuple[EnabledProvider, ...]:
    """Return the providers that may be collected, aggregated, or served."""

    # CONTRACT: the same list drives collection and display. Disabled IDs get
    # zero new I/O and must not appear in GET /usage.
    return tuple(settings.enabled_providers)


def is_complete_enabled_snapshot(snapshot: UsageSnapshot, enabled: Sequence[str]) -> bool:
    """True when the snapshot map is exactly the enabled set and that set is non-empty."""

    return bool(enabled) and set(snapshot.providers.keys()) == set(enabled)


def usage_error(
    *,
    settings: Settings | None,
    snapshot: UsageSnapshot | None,
    internal_failure: bool = False,
) -> ServiceError | None:
    """Return the GET /usage 503 body, or None when a v0.1 snapshot may be served."""

    # FALLBACK: config damage is not an empty enablement list. Empty list is
    # no_enabled_providers; unreadable settings is snapshot_unavailable.
    if settings is None:
        return SNAPSHOT_UNAVAILABLE
    if not settings.enabled_providers:
        return NO_ENABLED_PROVIDERS
    if internal_failure:
        return SNAPSHOT_UNAVAILABLE
    if snapshot is None or not is_complete_enabled_snapshot(snapshot, settings.enabled_providers):
        return SNAPSHOT_NOT_READY
    return None


def usage_error_envelope(
    *,
    settings: Settings | None,
    snapshot: UsageSnapshot | None,
    internal_failure: bool = False,
) -> ErrorEnvelope | None:
    error = usage_error(
        settings=settings,
        snapshot=snapshot,
        internal_failure=internal_failure,
    )
    if error is None:
        return None
    return ErrorEnvelope(error=error)


def health_document(*, settings: Settings | None) -> HealthDocument:
    """Process-liveness document. Provider quota health lives in GET /usage."""

    if settings is None:
        return HealthDocument(state="degraded", error=CONFIG_ERROR)
    if not settings.enabled_providers:
        return HealthDocument(state="idle")
    return HealthDocument(state="ready")
