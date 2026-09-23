"""Claude Code structured status-line adapter（PR #4）。

Responsibility: parse Claude Code status-line stdin JSON into typed collection
results. Non-goals: Claude login, Anthropic API calls, Collector scheduling,
or writing last-good cache. This CLI is a Collector parser, not the visible
Claude Code status line.

Inputs: a single JSON object from status-line stdin. Units are used percentage
0–100 and `resets_at` UTC Unix seconds. Outputs: ProviderCollectionSuccess
with `five_hour` / `weekly` quota meters, or ProviderCollectionFailure.

Contract: `docs/providers/claude.md` and `docs/contracts/provider-adapter.md`.
Retry, cache, and last-good fallback belong to the orchestrator.
"""

from __future__ import annotations

import json
import math
import sys
import time
from typing import BinaryIO, NoReturn, TextIO

from agent_meter.cache import ProviderCollectionFailure, ProviderCollectionSuccess
from agent_meter.models import QuotaMeter
from agent_meter.providers.result_dump import dump_collection_result

CLAUDE_PROVIDER_ID = "claude"
CLAUDE_SOURCE = "claude_statusline"
DEFAULT_MAX_STDIN_BYTES = 1_048_576

# PROVIDER: Claude Code documents five_hour + seven_day windows; normalized
# weekly meter id stays `weekly` to match the usage snapshot fixtures（PR #4）。
_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("five_hour", "five_hour", "5 hour"),
    ("seven_day", "weekly", "Weekly"),
)

_FAILURE_MESSAGES = {
    "empty_input": "Claude status-line input is empty",
    "malformed_json": "Claude status-line input is not valid JSON",
    "extra_data": "Claude status-line input contains extra data after JSON",
    "not_object": "Claude status-line input is not a JSON object",
    "missing_rate_limits": "Claude status-line input is missing rate_limits",
    "no_valid_meters": "Claude status-line input has no valid rate-limit meters",
    "input_too_large": "Claude status-line input exceeds the size limit",
}


def collect_statusline(
    payload: object, *, now: int
) -> ProviderCollectionSuccess | ProviderCollectionFailure:
    """Parse a decoded status-line JSON value. Unknown fields are ignored."""

    if not isinstance(payload, dict):
        return _failure("not_object")
    if "rate_limits" not in payload or payload["rate_limits"] is None:
        # PROVIDER: rate_limits is omitted or null until a session has a fresh
        # API response, and may stay missing on some accounts（PR #4）。
        return _failure("missing_rate_limits")
    rate_limits = payload["rate_limits"]
    if not isinstance(rate_limits, dict):
        return _failure("no_valid_meters")

    meters: list[QuotaMeter] = []
    for raw_id, meter_id, label in _WINDOWS:
        meter = _parse_window(rate_limits.get(raw_id), meter_id=meter_id, label=label)
        if meter is not None:
            meters.append(meter)
    if not meters:
        # FALLBACK: do not invent 0%/100% when every window is absent or
        # malformed. Orchestrator keeps last-good data if it has any（PR #4）。
        return _failure("no_valid_meters")
    return ProviderCollectionSuccess(
        provider_id=CLAUDE_PROVIDER_ID,
        source=CLAUDE_SOURCE,
        collected_at=now,
        meters=tuple(meters),
    )


def collect_statusline_bytes(
    raw: bytes, *, now: int
) -> ProviderCollectionSuccess | ProviderCollectionFailure:
    """Decode bounded stdin bytes, then parse. Never stringify the payload."""

    decoded = _decode_json_object(raw)
    if isinstance(decoded, ProviderCollectionFailure):
        return decoded
    return collect_statusline(decoded, now=now)


def ingest(
    stdin: BinaryIO,
    stdout: TextIO,
    stderr: TextIO,
    *,
    now: int,
    max_bytes: int = DEFAULT_MAX_STDIN_BYTES,
) -> int:
    """Read stdin, write one JSON result to stdout, diagnostics to stderr."""

    # CONTRACT: stdout is the machine-readable channel. Human diagnostics
    # stay on stderr even when the typed result is a failure（PR #4）。
    raw = stdin.read(max_bytes + 1)
    if len(raw) > max_bytes:
        result: ProviderCollectionSuccess | ProviderCollectionFailure = _failure("input_too_large")
    else:
        result = collect_statusline_bytes(raw, now=now)
    stdout.write(
        json.dumps(dump_collection_result(result), separators=(",", ":"), ensure_ascii=True)
    )
    stdout.write("\n")
    if isinstance(result, ProviderCollectionFailure):
        stderr.write(f"claude: {result.category}: {result.message}\n")
        return 1
    return 0


def main() -> int:
    """CLI entry for `python -m agent_meter.providers.claude`."""

    return ingest(sys.stdin.buffer, sys.stdout, sys.stderr, now=int(time.time()))


def _decode_json_object(raw: bytes) -> object | ProviderCollectionFailure:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # SECURITY: do not include undecodable bytes or JSONDecodeError
        # snippets; they can echo planted secrets from malformed input（PR #4）。
        return _failure("malformed_json")
    stripped = text.strip()
    if not stripped:
        return _failure("empty_input")
    decoder = json.JSONDecoder(parse_constant=_reject_nonstandard_constant)
    try:
        parsed, end = decoder.raw_decode(stripped)
    except (json.JSONDecodeError, ValueError, RecursionError):
        # CONTRACT: stdout 必須仍能走出 typed failure。超大整數是 ValueError、
        # 過深巢狀是 RecursionError；標準 JSON 也沒有 NaN／Infinity（PR #4）。
        return _failure("malformed_json")
    if stripped[end:].strip():
        return _failure("extra_data")
    value: object = parsed
    return value


def _reject_nonstandard_constant(_literal: str) -> NoReturn:
    # CONTRACT: RFC 8259 JSON 沒有 NaN／Infinity；不得因 unknown field
    # 裡的非標準常數而誤判 success（PR #4）。
    raise ValueError("non-standard JSON constant")


def _parse_window(window: object, *, meter_id: str, label: str) -> QuotaMeter | None:
    if not isinstance(window, dict):
        return None
    remaining = _remaining_percentage(window.get("used_percentage"))
    if remaining is None:
        # CONTRACT: missing, non-numeric, or out-of-range used_percentage is
        # skipped, never clamped to 0 or 100（PR #4）。
        return None
    meter_kwargs: dict[str, object] = {
        "id": meter_id,
        "label": label,
        "kind": "quota",
        "unit": "percent",
        "remaining_percentage": remaining,
    }
    reset_at = _unix_seconds(window.get("resets_at"))
    if reset_at is not None:
        meter_kwargs["reset_at"] = reset_at
    return QuotaMeter.model_validate(meter_kwargs)


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
    # PROVIDER: Claude Code documents resets_at as UTC Unix seconds. Invalid
    # or missing values omit reset_at; they do not reject a valid percentage（PR #4）。
    if value is None or type(value) is bool:
        return None
    if type(value) is int:
        return value if value >= 0 else None
    if type(value) is float and math.isfinite(value) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _failure(code: str) -> ProviderCollectionFailure:
    return ProviderCollectionFailure(
        provider_id=CLAUDE_PROVIDER_ID,
        source=CLAUDE_SOURCE,
        category="malformed_response",
        message=_FAILURE_MESSAGES[code],
        retryable=False,
        code=code,
    )


if __name__ == "__main__":
    raise SystemExit(main())
