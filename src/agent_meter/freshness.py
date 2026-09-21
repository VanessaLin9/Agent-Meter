"""Freshness and stale-threshold policy（PR #3）。

Responsibility: decide whether last-good provider data is still fresh using an
injected clock and configurable thresholds. Non-goals: adapter transport,
filesystem cache, HTTP, or choosing retry.

Inputs: `collected_at` and `stale_after_seconds` as UTC Unix seconds; `now`
from an injected clock. Outputs: a boolean freshness bit, or a provider
snapshot whose `ok` status has been recomputed to `stale`.

Contract: `docs/contracts/usage-api.md`. Retry I/O belongs to the orchestrator.
This module only classifies age against a threshold.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from agent_meter.models import ErrorSummary, ProviderOk, ProviderSnapshot, ProviderStale

# CONTRACT: Default lives in settings, never in a provider adapter（PR #3）。
DEFAULT_STALE_AFTER_SECONDS = 900

THRESHOLD_STALE_ERROR = ErrorSummary(
    category="cache",
    message="Provider data exceeded stale threshold",
    retryable=True,
    code="stale_threshold",
)


class Clock(Protocol):
    """UTC Unix-seconds clock. Production and tests inject this; never call time.time()."""

    def now(self) -> int:
        """Return UTC Unix seconds."""


@dataclass(frozen=True)
class FrozenClock:
    """Fixed clock for tests and deterministic evaluation. UTC Unix seconds."""

    unix_seconds: int

    def now(self) -> int:
        return self.unix_seconds


@dataclass(frozen=True)
class StaleSettings:
    """Per-provider stale thresholds with a config default."""

    default_stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS
    overrides: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "default_stale_after_seconds",
            _require_positive_int(self.default_stale_after_seconds, "default_stale_after_seconds"),
        )
        frozen: dict[str, int] = {}
        for provider_id, value in self.overrides.items():
            frozen[provider_id] = _require_positive_int(
                value, f"stale_after_seconds for {provider_id}"
            )
        # CONTRACT: overrides 必須是不可變 mapping，避免 frozen dataclass
        # 驗證後還能被改成 0（PR #3）。
        object.__setattr__(self, "overrides", MappingProxyType(frozen))

    def for_provider(self, provider_id: str) -> int:
        return self.overrides.get(provider_id, self.default_stale_after_seconds)


def _require_positive_int(value: object, name: str) -> int:
    # CONTRACT: bool 是 int 子類別，1.5 也會通過 `>= 1`；threshold 只接受
    # 嚴格的正整數（PR #3）。
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive int")
    return value


def is_fresh(*, collected_at: int | None, stale_after_seconds: int, now: int) -> bool:
    """Return whether last-good data is still fresh at `now`."""

    if collected_at is None:
        return False
    _require_positive_int(stale_after_seconds, "stale_after_seconds")
    # CONTRACT: stale when now >= collected_at + stale_after_seconds（PR #3）。
    # FALLBACK: now < collected_at（時鐘倒退）尚未過期，維持 fresh，避免誤標 stale（PR #3）。
    return now < collected_at + stale_after_seconds


def evaluate_provider(snapshot: ProviderSnapshot, *, now: int) -> ProviderSnapshot:
    """Recompute age-based stale on last-good data. Do not invent meters."""

    if not isinstance(snapshot, ProviderOk):
        # CONTRACT: refresh-failed stale / unavailable / error 不因仍在 threshold
        # 內被升回 ok；只有新的 success 才能恢復 ok（PR #3）。
        return snapshot
    if is_fresh(
        collected_at=snapshot.collected_at,
        stale_after_seconds=snapshot.stale_after_seconds,
        now=now,
    ):
        return snapshot
    # FALLBACK: 過期只改 status 與 sanitized cache error，保留原 meters 與
    # collected_at，避免用空值覆蓋 last-good（PR #3）。
    return ProviderStale(
        status="stale",
        source=snapshot.source,
        collected_at=snapshot.collected_at,
        stale_after_seconds=snapshot.stale_after_seconds,
        meters=list(snapshot.meters),
        error=THRESHOLD_STALE_ERROR,
    )
