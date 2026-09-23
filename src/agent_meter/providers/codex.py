"""Codex app-server rate-limit adapter（PR #5）。

Responsibility: map `account/rateLimits/read` into typed collection results,
and optionally run that call over the JSON-RPC transport. Non-goals: Codex
login, private OpenAI backends, Collector scheduling, or last-good cache.

Inputs: a JSON-RPC result object, or a spawned `codex app-server --stdio`
process. Units are used percent 0–100 and `resetsAt` UTC Unix seconds.
Outputs: ProviderCollectionSuccess with `five_hour` / `weekly` quota meters,
or ProviderCollectionFailure.

Contract: `docs/providers/codex.md` and `docs/contracts/provider-adapter.md`.
Retry, cache, and last-good fallback belong to the orchestrator.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Mapping, Sequence
from typing import TextIO

from agent_meter.cache import ProviderCollectionFailure, ProviderCollectionSuccess
from agent_meter.models import QuotaMeter
from agent_meter.providers.codex_rpc import (
    CODEX_PROVIDER_ID,
    CODEX_SOURCE,
    DEFAULT_DEADLINE_SECONDS,
    allowlisted_environ,
    default_codex_command,
    read_rate_limits_result,
)
from agent_meter.providers.result_dump import dump_collection_result

# PROVIDER: app-server names windows primary/secondary; normalized weekly
# meter id stays `weekly` to match the usage snapshot fixtures（PR #5）。
_WINDOWS: tuple[tuple[str, str, str, int], ...] = (
    ("primary", "five_hour", "5 hour", 300),
    ("secondary", "weekly", "Weekly", 10080),
)

_FAILURE_MESSAGES = {
    "not_object": "Codex rate-limit result is not a JSON object",
    "missing_rate_limits": "Codex rate-limit result is missing rateLimits",
    "no_valid_meters": "Codex rate-limit result has no valid quota meters",
}


def collect_rate_limits(
    payload: object, *, now: int
) -> ProviderCollectionSuccess | ProviderCollectionFailure:
    """Parse a decoded rate-limit JSON-RPC result. Unknown fields are ignored."""

    if not isinstance(payload, dict):
        return _mapping_failure("not_object")
    if "rateLimits" not in payload or payload["rateLimits"] is None:
        return _mapping_failure("missing_rate_limits")
    rate_limits = payload["rateLimits"]
    if not isinstance(rate_limits, dict):
        return _mapping_failure("no_valid_meters")

    meters: list[QuotaMeter] = []
    for raw_id, meter_id, label, expected_mins in _WINDOWS:
        meter = _parse_window(
            rate_limits.get(raw_id),
            meter_id=meter_id,
            label=label,
            expected_mins=expected_mins,
        )
        if meter is not None:
            meters.append(meter)
    if not meters:
        # FALLBACK: do not invent 0%/100% when every window is absent or
        # malformed. Orchestrator keeps last-good data if it has any（PR #5）。
        return _mapping_failure("no_valid_meters")
    return ProviderCollectionSuccess(
        provider_id=CODEX_PROVIDER_ID,
        source=CODEX_SOURCE,
        collected_at=now,
        meters=tuple(meters),
    )


def collect(
    *,
    now: int,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    command: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    stderr: TextIO | None = None,
) -> ProviderCollectionSuccess | ProviderCollectionFailure:
    """Spawn app-server, read rate limits, and map them. Never inherit full env."""

    child_env = allowlisted_environ() if env is None else dict(env)
    resolved = list(command) if command is not None else default_codex_command(child_env)
    if isinstance(resolved, ProviderCollectionFailure):
        return resolved
    deadline = time.monotonic() + deadline_seconds
    payload = read_rate_limits_result(resolved, env=child_env, deadline=deadline, stderr=stderr)
    if isinstance(payload, ProviderCollectionFailure):
        return payload
    # PROVIDER: rateLimitsByLimitId is ignored in v0.1; mapping is locked to
    # primary/secondary so a future lookup cannot silently replace meters.
    return collect_rate_limits(payload, now=now)


def main(argv: list[str] | None = None) -> int:
    """CLI entry for `python -m agent_meter.providers.codex`."""

    parser = argparse.ArgumentParser(prog="agent_meter.providers.codex")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Spawn the real Codex app-server. Required; default is offline.",
    )
    args = parser.parse_args(argv)
    if not args.live:
        # SECURITY: live provider access is opt-in. Default CLI must not spawn
        # app-server or read provider-owned login state（PR #5）。
        sys.stderr.write("codex: live app-server is opt-in; pass --live\n")
        return 2
    result = collect(now=int(time.time()), stderr=sys.stderr)
    sys.stdout.write(
        json.dumps(dump_collection_result(result), separators=(",", ":"), ensure_ascii=True)
    )
    sys.stdout.write("\n")
    if isinstance(result, ProviderCollectionFailure):
        sys.stderr.write(f"codex: {result.category}: {result.message}\n")
        return 1
    return 0


def _parse_window(
    window: object, *, meter_id: str, label: str, expected_mins: int
) -> QuotaMeter | None:
    if not isinstance(window, dict):
        return None
    if not _window_duration_matches(window.get("windowDurationMins"), expected_mins):
        # PROVIDER: windowDurationMins confirms identity. A mismatched duration
        # is an upgrade signal, not a different meter id（PR #5）。
        return None
    remaining = _remaining_percentage(window.get("usedPercent"))
    if remaining is None:
        # CONTRACT: missing, non-numeric, or out-of-range usedPercent is
        # skipped, never clamped to 0 or 100（PR #5）。
        return None
    meter_kwargs: dict[str, object] = {
        "id": meter_id,
        "label": label,
        "kind": "quota",
        "unit": "percent",
        "remaining_percentage": remaining,
    }
    reset_at = _unix_seconds(window.get("resetsAt"))
    if reset_at is not None:
        meter_kwargs["reset_at"] = reset_at
    return QuotaMeter.model_validate(meter_kwargs)


def _window_duration_matches(raw: object, expected: int) -> bool:
    if raw is None:
        return True
    minutes = _unix_seconds(raw)
    return minutes == expected


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


def _unix_seconds(value: object) -> int | None:
    # PROVIDER: resetsAt is documented as UTC Unix seconds. Millisecond-looking
    # values are omitted, not converted, so a unit change stays visible（PR #5）。
    if value is None or type(value) is bool:
        return None
    if type(value) is int:
        seconds = value
    elif type(value) is float and math.isfinite(value) and value.is_integer() and value >= 0:
        seconds = int(value)
    else:
        return None
    if seconds < 0 or seconds >= 10_000_000_000:
        return None
    return seconds


def _mapping_failure(code: str) -> ProviderCollectionFailure:
    return ProviderCollectionFailure(
        provider_id=CODEX_PROVIDER_ID,
        source=CODEX_SOURCE,
        category="malformed_response",
        message=_FAILURE_MESSAGES[code],
        retryable=False,
        code=code,
    )


if __name__ == "__main__":
    raise SystemExit(main())
