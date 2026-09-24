"""Cursor unofficial Connect RPC usage parser（checkpoint 1）。

Responsibility: map a sanitized `GetCurrentPeriodUsage` JSON object into typed
collection results. Non-goals: reading Cursor login state, building request
headers, network I/O, token refresh, Collector scheduling, and last-good cache.

Inputs: a decoded JSON object. Quota inputs are used percent 0–100.
`billingCycleEnd` is UTC Unix milliseconds. On-demand spend inputs are integer
cents. Outputs: ProviderCollectionSuccess with `cursor_models` / `other_models`
quota meters and an optional `on_demand` spend meter, or
ProviderCollectionFailure.

Contract: `docs/providers/cursor.md` and `docs/contracts/provider-adapter.md`.
Retry, cache, and last-good fallback belong to the orchestrator.
"""

from __future__ import annotations

import math

from agent_meter.cache import ProviderCollectionFailure, ProviderCollectionSuccess
from agent_meter.models import Meter, QuotaMeter, SpendMeter

CURSOR_PROVIDER_ID = "cursor"
CURSOR_SOURCE = "cursor_dashboard_connect_rpc"

# PROVIDER: values below this are seconds, not the milliseconds Cursor documents.
# Dividing them would land near 1970, so reset_at is omitted instead.
_MILLISECOND_THRESHOLD = 10_000_000_000

_POOLS: tuple[tuple[str, str, str], ...] = (
    ("autoPercentUsed", "cursor_models", "Cursor Models"),
    ("apiPercentUsed", "other_models", "Other Models"),
)

_FAILURE_MESSAGES = {
    "not_object": "Cursor period-usage result is not a JSON object",
    "missing_plan_usage": "Cursor period-usage result is missing planUsage",
    "no_valid_meters": "Cursor period-usage result has no valid quota meters",
}


def collect_period_usage(
    payload: object, *, now: int
) -> ProviderCollectionSuccess | ProviderCollectionFailure:
    """Parse a decoded GetCurrentPeriodUsage body. Unknown fields are ignored."""

    if not isinstance(payload, dict):
        return _mapping_failure("not_object")
    if "planUsage" not in payload or payload["planUsage"] is None:
        return _mapping_failure("missing_plan_usage")
    plan_usage = payload["planUsage"]
    if not isinstance(plan_usage, dict):
        return _mapping_failure("no_valid_meters")

    reset_at = _billing_cycle_end_seconds(payload.get("billingCycleEnd"))
    meters: list[Meter] = []
    for raw_key, meter_id, label in _POOLS:
        meter = _parse_pool(
            plan_usage.get(raw_key),
            meter_id=meter_id,
            label=label,
            reset_at=reset_at,
        )
        if meter is not None:
            meters.append(meter)
    if not meters:
        # FALLBACK: a bad pool is omitted. Do not invent 0% and do not keep a
        # spend-only result when both quota pools are unusable.
        return _mapping_failure("no_valid_meters")

    # PROVIDER: autoSpend / apiSpend stay unmapped. Included-pool usage is the
    # percentage meters; those cent fields are not a second currency meter.
    spend = _parse_on_demand(payload.get("spendLimitUsage"), reset_at=reset_at)
    if spend is not None:
        meters.append(spend)
    return ProviderCollectionSuccess(
        provider_id=CURSOR_PROVIDER_ID,
        source=CURSOR_SOURCE,
        collected_at=now,
        meters=tuple(meters),
    )


def _parse_pool(
    used_percent: object, *, meter_id: str, label: str, reset_at: int | None
) -> QuotaMeter | None:
    remaining = _remaining_percentage(used_percent)
    if remaining is None:
        # CONTRACT: each pool is judged alone. Missing, non-numeric, or
        # out-of-range percent is skipped, never clamped to 0 or 100.
        return None
    meter_kwargs: dict[str, object] = {
        "id": meter_id,
        "label": label,
        "kind": "quota",
        "unit": "percent",
        "remaining_percentage": remaining,
    }
    if reset_at is not None:
        meter_kwargs["reset_at"] = reset_at
    return QuotaMeter.model_validate(meter_kwargs)


def _parse_on_demand(spend_limit: object, *, reset_at: int | None) -> SpendMeter | None:
    if not isinstance(spend_limit, dict):
        return None
    used = _cents_to_usd(spend_limit.get("individualUsed"))
    if used is None:
        # PROVIDER: no spent amount means no on-demand meter, even when a limit
        # is present. Zero cents is a real zero and still produces a meter.
        return None
    meter_kwargs: dict[str, object] = {
        "id": "on_demand",
        "label": "On-demand",
        "kind": "spend",
        "unit": "currency",
        "used": used,
        "currency_code": "USD",
    }
    limit = _positive_usd(spend_limit.get("individualLimit"))
    if limit is not None:
        meter_kwargs["limit"] = limit
    remaining = _cents_to_usd(spend_limit.get("individualRemaining"))
    if remaining is not None:
        meter_kwargs["remaining"] = remaining
    if reset_at is not None:
        meter_kwargs["reset_at"] = reset_at
    return SpendMeter.model_validate(meter_kwargs)


def _remaining_percentage(used: object) -> int | float | None:
    if type(used) is bool or used is None:
        return None
    if type(used) is int:
        if 0 <= used <= 100:
            return 100 - used
        return None
    if type(used) is float:
        if not math.isfinite(used) or used < 0 or used > 100:
            return None
        return 100.0 - used
    return None


def _billing_cycle_end_seconds(value: object) -> int | None:
    raw = _non_negative_int(value)
    if raw is None or raw < _MILLISECOND_THRESHOLD:
        return None
    return raw // 1000


def _cents_to_usd(value: object) -> int | float | None:
    cents = _non_negative_int(value)
    if cents is None:
        return None
    if cents % 100 == 0:
        return cents // 100
    return cents / 100


def _positive_usd(value: object) -> int | float | None:
    amount = _cents_to_usd(value)
    if amount is None or amount <= 0:
        return None
    return amount


def _non_negative_int(value: object) -> int | None:
    if value is None or type(value) is bool:
        return None
    if type(value) is int:
        number = value
    elif type(value) is float and math.isfinite(value) and value.is_integer() and value >= 0:
        number = int(value)
    else:
        return None
    if number < 0:
        return None
    return number


def _mapping_failure(code: str) -> ProviderCollectionFailure:
    return ProviderCollectionFailure(
        provider_id=CURSOR_PROVIDER_ID,
        source=CURSOR_SOURCE,
        category="malformed_response",
        message=_FAILURE_MESSAGES[code],
        retryable=False,
        code=code,
    )
