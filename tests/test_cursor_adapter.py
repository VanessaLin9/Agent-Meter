"""Cursor period-usage parser tests.

Clock, Cursor login state, and Connect RPC are never used.
Values are fictional. Transport belongs to a later checkpoint.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from agent_meter.cache import (
    ProviderCollectionFailure,
    ProviderCollectionSuccess,
    apply_collection_result,
)
from agent_meter.freshness import StaleSettings
from agent_meter.models import QuotaMeter, SpendMeter
from agent_meter.providers.cursor import (
    CURSOR_PROVIDER_ID,
    CURSOR_SOURCE,
    collect_period_usage,
)
from agent_meter.providers.result_dump import dump_collection_result

NOW = 2_000_000_000
FAKE_SECRET = "PLANTED_PROVIDER_TOKEN_VALUE_DO_NOT_EMIT"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "providers" / "cursor"


def _fixture(name: str) -> object:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _quota(result: ProviderCollectionSuccess, meter_id: str) -> QuotaMeter:
    matched = [meter for meter in result.meters if meter.id == meter_id]
    assert len(matched) == 1
    meter = matched[0]
    assert isinstance(meter, QuotaMeter)
    return meter


def _spend(result: ProviderCollectionSuccess) -> SpendMeter:
    matched = [meter for meter in result.meters if meter.id == "on_demand"]
    assert len(matched) == 1
    meter = matched[0]
    assert isinstance(meter, SpendMeter)
    return meter


def test_happy_period_usage_maps_pools_reset_and_on_demand_spend() -> None:
    result = collect_period_usage(_fixture("happy.json"), now=NOW)

    assert isinstance(result, ProviderCollectionSuccess)
    assert result.provider_id == CURSOR_PROVIDER_ID
    assert result.source == CURSOR_SOURCE
    assert result.collected_at == NOW
    assert [meter.id for meter in result.meters] == [
        "cursor_models",
        "other_models",
        "on_demand",
    ]

    cursor_models = _quota(result, "cursor_models")
    assert cursor_models.label == "Cursor Models"
    assert cursor_models.kind == "quota"
    assert cursor_models.unit == "percent"
    assert cursor_models.remaining_percentage == 82
    assert cursor_models.reset_at == 2_000_600_000

    other_models = _quota(result, "other_models")
    assert other_models.label == "Other Models"
    assert other_models.remaining_percentage == 95
    assert other_models.reset_at == 2_000_600_000

    spend = _spend(result)
    assert spend.label == "On-demand"
    assert spend.kind == "spend"
    assert spend.unit == "currency"
    assert spend.used == 4.5
    assert spend.currency_code == "USD"
    assert spend.limit == 20
    assert spend.remaining == 15.5
    assert spend.reset_at == 2_000_600_000
    assert "used" not in cursor_models.model_dump(exclude_unset=True)

    dumped = dump_collection_result(result)
    serialized = json.dumps(dumped)
    assert FAKE_SECRET not in serialized
    assert "planUsage" not in dumped
    assert "spendLimitUsage" not in dumped
    assert "email" not in dumped
    assert dumped["result"] == "success"


def test_invalid_pool_is_omitted_and_the_other_pool_remains() -> None:
    result = collect_period_usage(_fixture("one-pool.json"), now=NOW)

    assert isinstance(result, ProviderCollectionSuccess)
    assert [meter.id for meter in result.meters] == ["cursor_models"]
    assert _quota(result, "cursor_models").remaining_percentage == 90
    assert _quota(result, "cursor_models").reset_at == 2_000_600_000


def test_missing_on_demand_used_omits_spend_meter() -> None:
    result = collect_period_usage(_fixture("no-spend.json"), now=NOW)

    assert isinstance(result, ProviderCollectionSuccess)
    assert [meter.id for meter in result.meters] == ["cursor_models", "other_models"]
    assert _quota(result, "cursor_models").remaining_percentage == 60
    assert _quota(result, "other_models").remaining_percentage == 85


def test_zero_on_demand_cents_is_a_real_zero() -> None:
    result = collect_period_usage(
        {
            "planUsage": {"autoPercentUsed": 1, "apiPercentUsed": 2},
            "spendLimitUsage": {"individualUsed": 0, "individualLimit": 0},
        },
        now=NOW,
    )

    assert isinstance(result, ProviderCollectionSuccess)
    spend = _spend(result)
    assert spend.used == 0
    assert "limit" not in spend.model_dump(exclude_unset=True)


def test_percentage_boundaries_zero_and_full_are_valid() -> None:
    result = collect_period_usage(
        {"planUsage": {"autoPercentUsed": 0, "apiPercentUsed": 100}},
        now=NOW,
    )

    assert isinstance(result, ProviderCollectionSuccess)
    assert _quota(result, "cursor_models").remaining_percentage == 100
    assert _quota(result, "other_models").remaining_percentage == 0


def test_float_used_percent_is_not_clamped() -> None:
    result = collect_period_usage(
        {"planUsage": {"autoPercentUsed": 0.5, "apiPercentUsed": 99.5}},
        now=NOW,
    )

    assert isinstance(result, ProviderCollectionSuccess)
    assert _quota(result, "cursor_models").remaining_percentage == 99.5
    assert _quota(result, "other_models").remaining_percentage == 0.5


def test_bad_percentage_types_skip_only_that_pool() -> None:
    for used in (True, False, "18", math.nan, math.inf, -1, None, 100.1):
        result = collect_period_usage(
            {"planUsage": {"autoPercentUsed": used, "apiPercentUsed": 12}},
            now=NOW,
        )
        assert isinstance(result, ProviderCollectionSuccess)
        assert [meter.id for meter in result.meters] == ["other_models"]
        assert _quota(result, "other_models").remaining_percentage == 88


def test_both_pools_invalid_is_malformed_even_with_spend() -> None:
    result = collect_period_usage(
        {
            "email": FAKE_SECRET,
            "planUsage": {"autoPercentUsed": 101, "apiPercentUsed": None},
            "spendLimitUsage": {"individualUsed": 450},
        },
        now=NOW,
    )

    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "malformed_response"
    assert result.code == "no_valid_meters"
    assert result.retryable is False
    assert result.provider_id == CURSOR_PROVIDER_ID
    assert result.source == CURSOR_SOURCE
    assert FAKE_SECRET not in result.message
    assert "\n" not in result.message


def test_empty_malformed_and_missing_plan_usage_fail_closed() -> None:
    cases = [
        ([], "not_object"),
        ("cursor", "not_object"),
        ({}, "missing_plan_usage"),
        ({"planUsage": None}, "missing_plan_usage"),
        ({"planUsage": []}, "no_valid_meters"),
        ({"planUsage": {}}, "no_valid_meters"),
        ({"planUsage": {"autoPercentUsed": None, "apiPercentUsed": {}}}, "no_valid_meters"),
    ]
    for payload, code in cases:
        result = collect_period_usage(payload, now=NOW)
        assert isinstance(result, ProviderCollectionFailure), code
        assert result.category == "malformed_response"
        assert result.code == code
        assert FAKE_SECRET not in result.message


def test_millisecond_billing_cycle_converts_and_seconds_are_omitted() -> None:
    converted = collect_period_usage(
        {
            "billingCycleEnd": 2_000_600_000_000.0,
            "planUsage": {"autoPercentUsed": 20, "apiPercentUsed": 30},
        },
        now=NOW,
    )
    assert isinstance(converted, ProviderCollectionSuccess)
    assert _quota(converted, "cursor_models").reset_at == 2_000_600_000
    assert _quota(converted, "other_models").reset_at == 2_000_600_000

    seconds = collect_period_usage(
        {
            "billingCycleEnd": 2_000_600_000,
            "planUsage": {"autoPercentUsed": 20, "apiPercentUsed": True},
        },
        now=NOW,
    )
    assert isinstance(seconds, ProviderCollectionSuccess)
    assert [meter.id for meter in seconds.meters] == ["cursor_models"]
    assert "reset_at" not in _quota(seconds, "cursor_models").model_dump(exclude_unset=True)


def test_invalid_spend_fields_do_not_drop_quota_meters() -> None:
    result = collect_period_usage(
        {
            "planUsage": {"autoPercentUsed": 25},
            "spendLimitUsage": {
                "individualUsed": True,
                "individualLimit": "20",
                "individualRemaining": -1,
            },
        },
        now=NOW,
    )

    assert isinstance(result, ProviderCollectionSuccess)
    assert [meter.id for meter in result.meters] == ["cursor_models"]


def test_one_cent_stays_a_fractional_dollar() -> None:
    result = collect_period_usage(
        {
            "planUsage": {"apiPercentUsed": 0},
            "spendLimitUsage": {"individualUsed": 1, "individualRemaining": 99},
        },
        now=NOW,
    )

    assert isinstance(result, ProviderCollectionSuccess)
    spend = _spend(result)
    assert spend.used == 0.01
    assert spend.remaining == 0.99
    assert _quota(result, "other_models").remaining_percentage == 100


def test_failure_lets_orchestrator_keep_last_good_snapshot() -> None:
    previous_success = collect_period_usage(_fixture("happy.json"), now=NOW - 10)
    assert isinstance(previous_success, ProviderCollectionSuccess)
    previous = apply_collection_result(None, previous_success, settings=StaleSettings())

    failure = collect_period_usage({"planUsage": {}}, now=NOW)
    assert isinstance(failure, ProviderCollectionFailure)
    merged = apply_collection_result(previous, failure, settings=StaleSettings())

    assert merged.status == "stale"
    assert [meter.id for meter in merged.meters] == [
        "cursor_models",
        "other_models",
        "on_demand",
    ]
    assert merged.collected_at == NOW - 10
    assert FAKE_SECRET not in merged.error.message
