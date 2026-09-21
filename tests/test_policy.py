"""Status aggregation, freshness, and last-valid cache policy tests.

Clock, filesystem, and provider I/O are never used. Values are fictional.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_meter.aggregation import ProviderStatus, aggregate_status, build_snapshot
from agent_meter.cache import (
    MALFORMED_PROVIDER_ERROR,
    MALFORMED_SNAPSHOT_ERROR,
    CacheEnvelope,
    ProviderCollectionFailure,
    ProviderCollectionSuccess,
    apply_collection_result,
    apply_snapshot_payload,
    evaluate_cached_snapshot,
    merge_collection_results,
)
from agent_meter.freshness import (
    DEFAULT_STALE_AFTER_SECONDS,
    THRESHOLD_STALE_ERROR,
    FrozenClock,
    StaleSettings,
    evaluate_provider,
    is_fresh,
)
from agent_meter.models import (
    ErrorSummary,
    ProviderError,
    ProviderOk,
    ProviderSnapshot,
    ProviderStale,
    ProviderUnavailable,
    QuotaMeter,
    dump_usage_snapshot,
    parse_usage_snapshot,
)

NOW = 2_000_000_000
FAKE_SECRET = "PLANTED_PROVIDER_TOKEN_VALUE_DO_NOT_EMIT"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "contracts"


def _quota(*, remaining_percentage: float = 95) -> QuotaMeter:
    return QuotaMeter(
        id="weekly",
        label="Weekly",
        kind="quota",
        unit="percent",
        remaining_percentage=remaining_percentage,
        reset_at=NOW + 600,
    )


def _ok(
    *,
    source: str = "codex_app_server",
    collected_at: int = NOW - 10,
    stale_after_seconds: int = 600,
    remaining_percentage: float = 95,
) -> ProviderOk:
    return ProviderOk(
        status="ok",
        source=source,
        collected_at=collected_at,
        stale_after_seconds=stale_after_seconds,
        meters=[_quota(remaining_percentage=remaining_percentage)],
    )


def _stale(
    *,
    source: str = "cursor_dashboard_connect_rpc",
    collected_at: int = NOW - 2_000,
    stale_after_seconds: int = 900,
) -> ProviderStale:
    return ProviderStale(
        status="stale",
        source=source,
        collected_at=collected_at,
        stale_after_seconds=stale_after_seconds,
        meters=[_quota(remaining_percentage=36.55)],
        error=ErrorSummary(
            category="timeout",
            message="Cursor usage request timed out",
            retryable=True,
        ),
    )


def _unavailable() -> ProviderUnavailable:
    return ProviderUnavailable(
        status="unavailable",
        source="claude_statusline",
        collected_at=None,
        stale_after_seconds=900,
        meters=[],
        error=ErrorSummary(
            category="not_authenticated",
            message="Claude Code session is unavailable",
            retryable=False,
        ),
    )


def _ok_payload() -> dict[str, object]:
    payload = json.loads((FIXTURE_DIR / "ok.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _fixture_statuses(name: str) -> dict[str, ProviderStatus]:
    payload = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    snapshot = parse_usage_snapshot(payload)
    return {provider_id: provider.status for provider_id, provider in snapshot.providers.items()}


def test_default_threshold_lives_in_settings_not_adapters() -> None:
    settings = StaleSettings()
    assert settings.default_stale_after_seconds == DEFAULT_STALE_AFTER_SECONDS
    assert settings.for_provider("codex") == DEFAULT_STALE_AFTER_SECONDS
    overridden = StaleSettings(overrides={"codex": 600})
    assert overridden.for_provider("codex") == 600
    assert overridden.for_provider("cursor") == DEFAULT_STALE_AFTER_SECONDS


def test_threshold_must_be_at_least_one_second() -> None:
    with pytest.raises(ValueError, match="default_stale_after_seconds"):
        StaleSettings(default_stale_after_seconds=0)
    with pytest.raises(ValueError, match="codex"):
        StaleSettings(overrides={"codex": 0})
    with pytest.raises(ValueError, match="stale_after_seconds"):
        is_fresh(collected_at=NOW, stale_after_seconds=0, now=NOW)


def test_fresh_inside_window_stale_at_and_after_boundary() -> None:
    collected_at = NOW
    stale_after = 600
    assert is_fresh(collected_at=collected_at, stale_after_seconds=stale_after, now=NOW + 599)
    assert not is_fresh(collected_at=collected_at, stale_after_seconds=stale_after, now=NOW + 600)
    assert not is_fresh(collected_at=collected_at, stale_after_seconds=stale_after, now=NOW + 601)


def test_missing_collected_at_is_not_fresh() -> None:
    assert not is_fresh(collected_at=None, stale_after_seconds=900, now=NOW)


def test_clock_going_backwards_stays_fresh() -> None:
    assert is_fresh(collected_at=NOW, stale_after_seconds=600, now=NOW - 86_400)


def test_evaluate_ok_provider_becomes_stale_at_threshold_and_keeps_meters() -> None:
    snapshot = _ok(collected_at=NOW, stale_after_seconds=600)
    still_ok = evaluate_provider(snapshot, now=NOW + 599)
    assert still_ok is snapshot

    aged = evaluate_provider(snapshot, now=NOW + 600)
    assert isinstance(aged, ProviderStale)
    assert aged.meters == snapshot.meters
    assert aged.collected_at == snapshot.collected_at
    assert aged.error == THRESHOLD_STALE_ERROR


def test_evaluate_does_not_promote_failed_refresh_back_to_ok() -> None:
    snapshot = _stale(collected_at=NOW - 10, stale_after_seconds=900)
    evaluated = evaluate_provider(snapshot, now=NOW)
    assert evaluated is snapshot
    assert evaluated.status == "stale"


def test_evaluate_leaves_unavailable_untouched() -> None:
    snapshot = _unavailable()
    assert evaluate_provider(snapshot, now=NOW) is snapshot


def test_frozen_clock_does_not_read_system_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom_time() -> float:
        raise AssertionError("system clock")

    monkeypatch.setattr(time, "time", boom_time)

    clock = FrozenClock(NOW)
    snapshot = _ok(collected_at=clock.now(), stale_after_seconds=600)
    evaluated = evaluate_provider(snapshot, now=clock.now() + 600)
    assert evaluated.status == "stale"


def test_all_fresh_is_ok() -> None:
    assert aggregate_status(_fixture_statuses("ok.json")) == "ok"
    assert aggregate_status({"codex": "ok", "cursor": "ok", "claude": "ok"}) == "ok"


def test_partial_stale_and_unavailable_is_partial() -> None:
    assert aggregate_status(_fixture_statuses("partial.json")) == "partial"


def test_all_unavailable_is_error() -> None:
    assert aggregate_status({"codex": "unavailable", "cursor": "unavailable"}) == "error"


def test_mixed_error_without_displayable_data_is_error() -> None:
    assert aggregate_status(_fixture_statuses("error.json")) == "error"
    assert aggregate_status({"codex": "error", "cursor": "unavailable"}) == "error"


def test_mixed_ok_and_error_is_partial() -> None:
    assert aggregate_status({"codex": "ok", "cursor": "error"}) == "partial"


def test_all_stale_is_partial_not_error() -> None:
    assert aggregate_status({"codex": "stale", "cursor": "stale"}) == "partial"


def test_no_providers_is_error() -> None:
    assert aggregate_status({}) == "error"


def test_build_snapshot_recomputes_freshness_and_generated_at() -> None:
    clock = FrozenClock(NOW)
    providers = {
        "codex": _ok(collected_at=NOW, stale_after_seconds=600),
        "cursor": _ok(
            source="cursor_dashboard_connect_rpc",
            collected_at=NOW,
            stale_after_seconds=900,
        ),
    }
    fresh = build_snapshot(providers, now=clock.now())
    assert fresh.status == "ok"
    assert fresh.generated_at == NOW

    aged = build_snapshot(providers, now=NOW + 600)
    assert aged.status == "partial"
    assert aged.generated_at == NOW + 600
    assert aged.providers["codex"].status == "stale"
    assert aged.providers["cursor"].status == "ok"


def test_empty_providers_cannot_build_snapshot() -> None:
    with pytest.raises(ValueError, match="providers must not be empty"):
        build_snapshot({}, now=NOW)


def test_one_provider_status_does_not_rewrite_another() -> None:
    providers: dict[str, ProviderSnapshot] = {
        "codex": _ok(),
        "cursor": _stale(),
        "claude": _unavailable(),
    }
    snapshot = build_snapshot(providers, now=NOW)
    assert snapshot.providers["codex"].status == "ok"
    assert snapshot.providers["cursor"].status == "stale"
    assert snapshot.providers["claude"].status == "unavailable"
    assert evaluate_provider(providers["codex"], now=NOW) is providers["codex"]
    assert snapshot.status == "partial"


def test_clock_going_backwards_keeps_ok_aggregation() -> None:
    providers = {
        "codex": _ok(collected_at=NOW, stale_after_seconds=600),
        "cursor": _ok(
            source="cursor_dashboard_connect_rpc",
            collected_at=NOW,
            stale_after_seconds=900,
        ),
    }
    snapshot = build_snapshot(providers, now=NOW - 10_000)
    assert snapshot.status == "ok"
    assert all(provider.status == "ok" for provider in snapshot.providers.values())


def test_malformed_snapshot_keeps_last_valid() -> None:
    original = parse_usage_snapshot(_ok_payload())
    envelope = CacheEnvelope(snapshot=original)
    updated = apply_snapshot_payload(envelope, {"not": "a snapshot", "token": FAKE_SECRET})
    assert updated.snapshot == original
    assert updated.last_error == MALFORMED_SNAPSHOT_ERROR
    dumped = json.dumps(updated.model_dump(mode="json"), default=str)
    assert FAKE_SECRET not in dumped


def test_malformed_snapshot_without_last_valid_does_not_invent_meters() -> None:
    updated = apply_snapshot_payload(CacheEnvelope(), {"status": "ok", "access_token": FAKE_SECRET})
    assert updated.snapshot is None
    assert updated.last_error == MALFORMED_SNAPSHOT_ERROR
    assert FAKE_SECRET not in json.dumps(updated.model_dump(mode="json"), default=str)


def test_valid_snapshot_replaces_cache_and_clears_error() -> None:
    envelope = CacheEnvelope(last_error=MALFORMED_SNAPSHOT_ERROR)
    updated = apply_snapshot_payload(envelope, _ok_payload())
    assert updated.snapshot is not None
    assert updated.snapshot.status == "ok"
    assert updated.last_error is None
    assert dump_usage_snapshot(updated.snapshot) == _ok_payload()


def test_envelope_rejects_credential_shaped_fields() -> None:
    with pytest.raises(ValidationError):
        CacheEnvelope.model_validate({"snapshot": None, "access_token": FAKE_SECRET})
    envelope = apply_snapshot_payload(
        CacheEnvelope(),
        {"schema_version": "0.1", "access_token": FAKE_SECRET},
    )
    assert envelope.snapshot is None
    assert FAKE_SECRET not in json.dumps(envelope.model_dump(mode="json"), default=str)


def test_success_uses_settings_threshold_not_adapter_constant() -> None:
    settings = StaleSettings(default_stale_after_seconds=900, overrides={"codex": 600})
    snapshot = apply_collection_result(
        None,
        ProviderCollectionSuccess(
            provider_id="codex",
            source="codex_app_server",
            collected_at=NOW,
            meters=(_quota(),),
        ),
        settings=settings,
    )
    assert isinstance(snapshot, ProviderOk)
    assert snapshot.stale_after_seconds == 600
    cursor = apply_collection_result(
        None,
        ProviderCollectionSuccess(
            provider_id="cursor",
            source="cursor_dashboard_connect_rpc",
            collected_at=NOW,
            meters=(_quota(),),
        ),
        settings=settings,
    )
    assert cursor.stale_after_seconds == 900


def test_failure_with_last_good_becomes_stale_and_keeps_meters() -> None:
    previous = _ok(collected_at=NOW - 30, remaining_percentage=82)
    snapshot = apply_collection_result(
        previous,
        ProviderCollectionFailure(
            provider_id="codex",
            source="codex_app_server",
            category="timeout",
            message="Codex rate-limit read timed out",
            retryable=True,
        ),
        settings=StaleSettings(),
    )
    assert isinstance(snapshot, ProviderStale)
    assert snapshot.meters == previous.meters
    assert snapshot.collected_at == previous.collected_at
    assert snapshot.error.category == "timeout"


def test_empty_success_meters_do_not_overwrite_last_good() -> None:
    previous = _ok(remaining_percentage=40)
    snapshot = apply_collection_result(
        previous,
        ProviderCollectionSuccess(
            provider_id="codex",
            source="codex_app_server",
            collected_at=NOW,
            meters=(),
        ),
        settings=StaleSettings(),
    )
    assert isinstance(snapshot, ProviderStale)
    assert snapshot.meters == previous.meters
    assert snapshot.collected_at == previous.collected_at
    assert snapshot.error == MALFORMED_PROVIDER_ERROR


def test_failure_without_last_good_is_error_or_unavailable() -> None:
    settings = StaleSettings()
    timeout = apply_collection_result(
        None,
        ProviderCollectionFailure(
            provider_id="codex",
            source="codex_app_server",
            category="timeout",
            message="Codex rate-limit read timed out",
            retryable=True,
        ),
        settings=settings,
    )
    assert isinstance(timeout, ProviderError)
    assert timeout.meters == []
    assert timeout.collected_at is None

    missing = apply_collection_result(
        None,
        ProviderCollectionFailure(
            provider_id="claude",
            source="claude_statusline",
            category="not_authenticated",
            message="Claude Code session is unavailable",
            retryable=False,
        ),
        settings=settings,
    )
    assert isinstance(missing, ProviderUnavailable)
    assert missing.meters == []


def test_single_provider_failure_does_not_change_other_providers() -> None:
    previous = {
        "codex": _ok(remaining_percentage=82),
        "cursor": _ok(
            source="cursor_dashboard_connect_rpc",
            remaining_percentage=36.55,
        ),
    }
    merged = merge_collection_results(
        previous,
        [
            ProviderCollectionFailure(
                provider_id="cursor",
                source="cursor_dashboard_connect_rpc",
                category="network",
                message="Cursor backend is unreachable",
                retryable=True,
            )
        ],
        settings=StaleSettings(),
    )
    assert merged["codex"] == previous["codex"]
    assert merged["cursor"].status == "stale"
    assert merged["cursor"].meters == previous["cursor"].meters
    assert aggregate_status({pid: snap.status for pid, snap in merged.items()}) == "partial"


def test_restore_from_cache_recomputes_stale_with_injected_clock() -> None:
    envelope = apply_snapshot_payload(CacheEnvelope(), _ok_payload())
    clock = FrozenClock(NOW)
    served = evaluate_cached_snapshot(envelope, now=clock.now())
    assert served is not None
    assert served.status == "ok"
    assert served.generated_at == NOW

    later = FrozenClock(2_000_000_580)
    aged = evaluate_cached_snapshot(envelope, now=later.now())
    assert aged is not None
    assert aged.providers["codex"].status == "stale"
    assert aged.providers["codex"].collected_at == 1_999_999_980
    assert aged.providers["codex"].meters
    assert aged.status == "partial"
    assert aged.generated_at == later.now()


def test_restore_without_cache_returns_none() -> None:
    assert evaluate_cached_snapshot(CacheEnvelope(), now=NOW) is None


def test_build_snapshot_from_merged_results_matches_contract_statuses() -> None:
    settings = StaleSettings(overrides={"codex": 600, "cursor": 900})
    merged = merge_collection_results(
        {},
        [
            ProviderCollectionSuccess(
                provider_id="codex",
                source="codex_app_server",
                collected_at=NOW,
                meters=(_quota(),),
            ),
            ProviderCollectionFailure(
                provider_id="cursor",
                source="cursor_dashboard_connect_rpc",
                category="timeout",
                message="Cursor usage request timed out",
                retryable=True,
            ),
        ],
        settings=settings,
    )
    snapshot = build_snapshot(merged, now=NOW)
    assert snapshot.status == "partial"
    assert snapshot.providers["codex"].status == "ok"
    assert snapshot.providers["cursor"].status == "error"
