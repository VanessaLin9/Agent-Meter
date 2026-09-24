"""Cursor unofficial Connect RPC usage adapter（PR #6）。

Responsibility: map a `GetCurrentPeriodUsage` JSON object into typed collection
results, and optionally fetch that object through the read-only Connect RPC
client. Non-goals: token refresh, Collector scheduling, and last-good cache.
Login state and request headers stay in `cursor_rpc.py`.

Inputs: a decoded JSON object, or a live collect with an injected state
directory and transport. Quota inputs are used percent 0–100.
`billingCycleEnd` is UTC Unix milliseconds. On-demand spend inputs are integer
cents. Outputs: ProviderCollectionSuccess with `cursor_models` / `other_models`
quota meters and an optional `on_demand` spend meter, or
ProviderCollectionFailure.

Contract: `docs/providers/cursor.md` and `docs/contracts/provider-adapter.md`.
Retry, cache, and last-good fallback belong to the orchestrator.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

from agent_meter.cache import ProviderCollectionFailure, ProviderCollectionSuccess
from agent_meter.models import Meter, QuotaMeter, SpendMeter
from agent_meter.providers.cursor_rpc import (
    CURSOR_PROVIDER_ID,
    CURSOR_SOURCE,
    DEFAULT_DEADLINE_SECONDS,
    CursorTransport,
    build_request,
    decode_period_usage_body,
    load_session,
    urllib_transport,
)
from agent_meter.providers.result_dump import dump_collection_result

__all__ = ["CURSOR_PROVIDER_ID", "CURSOR_SOURCE", "collect", "collect_period_usage"]

# PROVIDER: 小於這個值的 billingCycleEnd 看起來像秒。Cursor 文件是毫秒，
# 再除 1000 會落到 1970，所以省略 reset_at，不猜單位（PR #6）。
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


def collect(
    *,
    now: int,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    state_dir: Path | None = None,
    version_path: Path | None = None,
    client_version: str | None = None,
    transport: CursorTransport | None = None,
) -> ProviderCollectionSuccess | ProviderCollectionFailure:
    """Read the local session, call Connect RPC, and map the body.

    Tests inject `state_dir` and `transport`. The default path is live and
    read-only; it does not refresh tokens or write Cursor files.
    """

    material = load_session(
        state_dir=state_dir,
        version_path=version_path,
        client_version=client_version,
    )
    if isinstance(material, ProviderCollectionFailure):
        return material
    request = build_request(material)
    del material
    send = urllib_transport if transport is None else transport
    outcome = send(request, deadline_seconds=deadline_seconds)
    if isinstance(outcome, ProviderCollectionFailure):
        return outcome
    if outcome.status == 401:
        return ProviderCollectionFailure(
            provider_id=CURSOR_PROVIDER_ID,
            source=CURSOR_SOURCE,
            category="auth_expired",
            message="Cursor rejected the local login state",
            retryable=False,
            code="token_rejected",
        )
    if outcome.status != 200:
        return ProviderCollectionFailure(
            provider_id=CURSOR_PROVIDER_ID,
            source=CURSOR_SOURCE,
            category="upstream",
            message="Cursor period-usage request was not successful",
            retryable=outcome.status >= 500,
            code="upstream_http_error",
        )
    payload = decode_period_usage_body(outcome.body)
    if isinstance(payload, ProviderCollectionFailure):
        return payload
    return collect_period_usage(payload, now=now)


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
        # FALLBACK: 壞掉的 pool 省略，不補 0。兩個 quota pool 都無法使用時，
        # 即使 on-demand 有金額也不留下 spend-only success（PR #6）。
        return _mapping_failure("no_valid_meters")

    # PROVIDER: autoSpend／apiSpend 不放進 percent quota meter。Included pool
    # 的顯示值是 percentage；這些 cents 不是第二顆 currency meter（PR #6）。
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
        # CONTRACT: 兩個 pool 分開判斷。缺值、非數字或超出 0–100 只省略該 pool，
        # 不 clamp，也不把整個 Cursor 判成失敗（PR #6）。
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
        # PROVIDER: 沒有 individualUsed 就沒有 on-demand meter，即使 limit 有值。
        # 0 cents 是真實的 0，仍要產出 meter（PR #6）。
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
    # PROVIDER: on-demand upstream 是 cents。Normalized spend meter 用 USD
    # major units，避免和 percent quota meter 混單位（PR #6）。
    cents = _non_negative_int(value)
    if cents is None:
        return None
    if cents % 100 == 0:
        return cents // 100
    try:
        return cents / 100
    except OverflowError:
        # PROVIDER: 非整百且大到無法轉成 float 時省略該金額。optional spend
        # 不能用 OverflowError 吃掉仍然合法的 quota meters（PR #6）。
        return None


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


def main(argv: list[str] | None = None) -> int:
    """CLI entry for `python -m agent_meter.providers.cursor`."""

    parser = argparse.ArgumentParser(prog="agent_meter.providers.cursor")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Read the local Cursor session and call Connect RPC. Required.",
    )
    args = parser.parse_args(argv)
    if not args.live:
        # SECURITY: live provider access is opt-in. Default CLI must not read
        # Cursor local state or send the access token（PR #6）。
        sys.stderr.write("cursor: live Connect RPC is opt-in; pass --live\n")
        return 2
    result = collect(now=int(time.time()))
    sys.stdout.write(
        json.dumps(dump_collection_result(result), separators=(",", ":"), ensure_ascii=True)
    )
    sys.stdout.write("\n")
    if isinstance(result, ProviderCollectionFailure):
        sys.stderr.write(f"cursor: {result.category}: {result.message}\n")
        return 1
    return 0


def _mapping_failure(code: str) -> ProviderCollectionFailure:
    return ProviderCollectionFailure(
        provider_id=CURSOR_PROVIDER_ID,
        source=CURSOR_SOURCE,
        category="malformed_response",
        message=_FAILURE_MESSAGES[code],
        retryable=False,
        code=code,
    )


if __name__ == "__main__":
    raise SystemExit(main())
