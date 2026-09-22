"""Claude Code status-line adapter tests.

Clock, network, and live Claude sessions are never used. Values are fictional.
"""

from __future__ import annotations

import io
import json
import math
import subprocess
import sys
from pathlib import Path

from agent_meter.cache import (
    ProviderCollectionFailure,
    ProviderCollectionSuccess,
    apply_collection_result,
)
from agent_meter.freshness import FrozenClock, StaleSettings
from agent_meter.models import QuotaMeter
from agent_meter.providers.claude import (
    CLAUDE_PROVIDER_ID,
    CLAUDE_SOURCE,
    collect_statusline,
    collect_statusline_bytes,
    dump_collection_result,
    ingest,
)

NOW = 2_000_000_000
FAKE_SECRET = "PLANTED_PROVIDER_TOKEN_VALUE_DO_NOT_EMIT"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "providers" / "claude"


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def _meter_by_id(result: ProviderCollectionSuccess, meter_id: str) -> QuotaMeter:
    matched = [meter for meter in result.meters if meter.id == meter_id]
    assert len(matched) == 1
    meter = matched[0]
    assert isinstance(meter, QuotaMeter)
    return meter


def test_happy_statusline_maps_used_percentage_to_remaining_meters() -> None:
    result = collect_statusline_bytes(_fixture_bytes("happy.json"), now=NOW)

    assert isinstance(result, ProviderCollectionSuccess)
    assert result.provider_id == CLAUDE_PROVIDER_ID
    assert result.source == CLAUDE_SOURCE
    assert result.collected_at == NOW
    assert [meter.id for meter in result.meters] == ["five_hour", "weekly"]

    five_hour = _meter_by_id(result, "five_hour")
    assert five_hour.label == "5 hour"
    assert five_hour.kind == "quota"
    assert five_hour.unit == "percent"
    assert five_hour.remaining_percentage == 76.5
    assert five_hour.reset_at == 2_000_004_000

    weekly = _meter_by_id(result, "weekly")
    assert weekly.label == "Weekly"
    assert weekly.remaining_percentage == 58.8
    assert weekly.reset_at == 2_000_600_400

    dumped = dump_collection_result(result)
    serialized = json.dumps(dumped)
    assert FAKE_SECRET not in serialized
    assert "account" not in dumped
    assert dumped["result"] == "success"


def test_independently_absent_window_still_succeeds_with_one_meter() -> None:
    result = collect_statusline_bytes(_fixture_bytes("one-window.json"), now=NOW)

    assert isinstance(result, ProviderCollectionSuccess)
    assert [meter.id for meter in result.meters] == ["five_hour"]
    assert _meter_by_id(result, "five_hour").remaining_percentage == 90


def test_missing_or_null_resets_at_keeps_meter_without_reset() -> None:
    result = collect_statusline_bytes(_fixture_bytes("missing-reset.json"), now=NOW)

    assert isinstance(result, ProviderCollectionSuccess)
    five_hour = _meter_by_id(result, "five_hour")
    weekly = _meter_by_id(result, "weekly")
    assert five_hour.remaining_percentage == 60
    assert weekly.remaining_percentage == 85
    assert "reset_at" not in five_hour.model_dump(exclude_unset=True)
    assert "reset_at" not in weekly.model_dump(exclude_unset=True)


def test_decoded_object_collect_matches_bytes_path() -> None:
    payload = json.loads(_fixture_bytes("happy.json"))
    from_object = collect_statusline(payload, now=NOW)
    from_bytes = collect_statusline_bytes(_fixture_bytes("happy.json"), now=NOW)
    assert from_object == from_bytes


def test_integer_valued_reset_float_normalizes_to_unix_seconds() -> None:
    result = collect_statusline(
        {"rate_limits": {"five_hour": {"used_percentage": 1, "resets_at": 2000004000.0}}},
        now=NOW,
    )
    assert isinstance(result, ProviderCollectionSuccess)
    assert _meter_by_id(result, "five_hour").reset_at == 2_000_004_000


def test_invalid_resets_at_is_omitted_instead_of_rejecting_percentage() -> None:
    result = collect_statusline(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 20, "resets_at": 2000004000.5},
                "seven_day": {"used_percentage": 30, "resets_at": True},
            }
        },
        now=NOW,
    )
    assert isinstance(result, ProviderCollectionSuccess)
    assert "reset_at" not in _meter_by_id(result, "five_hour").model_dump(exclude_unset=True)
    assert "reset_at" not in _meter_by_id(result, "weekly").model_dump(exclude_unset=True)


def test_percentage_boundaries_zero_and_full_are_valid() -> None:
    result = collect_statusline(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 0, "resets_at": NOW + 1},
                "seven_day": {"used_percentage": 100, "resets_at": NOW + 2},
            }
        },
        now=NOW,
    )
    assert isinstance(result, ProviderCollectionSuccess)
    assert _meter_by_id(result, "five_hour").remaining_percentage == 100
    assert _meter_by_id(result, "weekly").remaining_percentage == 0


def test_out_of_range_or_non_numeric_percentage_skips_that_window() -> None:
    result = collect_statusline(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 101},
                "seven_day": {"used_percentage": 12},
            }
        },
        now=NOW,
    )
    assert isinstance(result, ProviderCollectionSuccess)
    assert [meter.id for meter in result.meters] == ["weekly"]
    assert _meter_by_id(result, "weekly").remaining_percentage == 88


def test_boolean_nan_and_string_percentages_are_not_coerced() -> None:
    for used in (True, False, "23.5", math.nan, math.inf, -1, None, 100.1):
        result = collect_statusline(
            {"rate_limits": {"five_hour": {"used_percentage": used}}},
            now=NOW,
        )
        assert isinstance(result, ProviderCollectionFailure)
        assert result.category == "malformed_response"
        assert result.code == "no_valid_meters"
        assert result.retryable is False
        assert FAKE_SECRET not in result.message


def test_empty_whitespace_malformed_and_missing_rate_limits_fail_closed() -> None:
    cases = [
        (b"", "empty_input"),
        (b" \n\t ", "empty_input"),
        (_fixture_bytes("malformed.json"), "malformed_json"),
        (_fixture_bytes("no-fresh-api-response.json"), "missing_rate_limits"),
        (_fixture_bytes("null-rate-limits.json"), "missing_rate_limits"),
        (b"[]", "not_object"),
        (b'"claude"', "not_object"),
        (b'{"rate_limits": []}', "no_valid_meters"),
        (b'{"rate_limits": {}}', "no_valid_meters"),
        (b'{"rate_limits": {"five_hour": null, "seven_day": {}}}', "no_valid_meters"),
        (b'{"rate_limits": {"five_hour": {"used_percentage": 1}}}{"x":1}', "extra_data"),
    ]
    for raw, code in cases:
        result = collect_statusline_bytes(raw, now=NOW)
        assert isinstance(result, ProviderCollectionFailure), code
        assert result.category == "malformed_response"
        assert result.code == code
        assert result.provider_id == CLAUDE_PROVIDER_ID
        assert result.source == CLAUDE_SOURCE
        assert FAKE_SECRET not in result.message
        assert "\n" not in result.message


def test_trailing_whitespace_after_json_is_allowed() -> None:
    raw = _fixture_bytes("one-window.json").rstrip() + b"\n\n"
    result = collect_statusline_bytes(raw, now=NOW)
    assert isinstance(result, ProviderCollectionSuccess)


def test_invalid_utf8_is_malformed_without_leaking_bytes() -> None:
    result = collect_statusline_bytes(b"\xff\xfe" + FAKE_SECRET.encode(), now=NOW)
    assert isinstance(result, ProviderCollectionFailure)
    assert result.code == "malformed_json"
    assert FAKE_SECRET not in result.message


def _huge_integer_payload() -> bytes:
    return (b"1" * 5000) + b"\n" + FAKE_SECRET.encode()


def _deeply_nested_payload() -> bytes:
    depth = 10_000
    return b'{"a":' * depth + json.dumps(FAKE_SECRET).encode() + b"}" * depth


def _nonstandard_constant_payload(constant: str) -> bytes:
    return (
        b'{"rate_limits":{"five_hour":{"used_percentage":10}},"extra":'
        + constant.encode()
        + b',"secret":"'
        + FAKE_SECRET.encode()
        + b'"}'
    )


def _assert_typed_malformed_channels(raw: bytes) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = ingest(io.BytesIO(raw), stdout, stderr, now=NOW)
    assert exit_code == 1
    payload = json.loads(stdout.getvalue())
    assert payload["result"] == "failure"
    assert payload["code"] == "malformed_json"
    assert FAKE_SECRET not in stdout.getvalue()
    assert FAKE_SECRET not in stderr.getvalue()

    completed = subprocess.run(
        [sys.executable, "-m", "agent_meter.providers.claude"],
        input=raw,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 1
    cli_payload = json.loads(completed.stdout.decode("utf-8"))
    assert cli_payload["result"] == "failure"
    assert cli_payload["code"] == "malformed_json"
    assert FAKE_SECRET not in completed.stdout.decode("utf-8")
    assert FAKE_SECRET not in completed.stderr.decode("utf-8")


def test_oversized_integer_json_stays_on_typed_failure_channel() -> None:
    _assert_typed_malformed_channels(_huge_integer_payload())


def test_deeply_nested_json_stays_on_typed_failure_channel() -> None:
    _assert_typed_malformed_channels(_deeply_nested_payload())


def test_nonstandard_json_constants_are_rejected_even_in_unknown_fields() -> None:
    for constant in ("NaN", "Infinity", "-Infinity"):
        _assert_typed_malformed_channels(_nonstandard_constant_payload(constant))


def test_failure_lets_orchestrator_keep_last_good_snapshot() -> None:
    previous_success = collect_statusline_bytes(_fixture_bytes("happy.json"), now=NOW - 10)
    assert isinstance(previous_success, ProviderCollectionSuccess)
    previous = apply_collection_result(None, previous_success, settings=StaleSettings())

    failure = collect_statusline_bytes(_fixture_bytes("no-fresh-api-response.json"), now=NOW)
    assert isinstance(failure, ProviderCollectionFailure)
    merged = apply_collection_result(previous, failure, settings=StaleSettings())

    assert merged.status == "stale"
    assert [meter.id for meter in merged.meters] == ["five_hour", "weekly"]
    assert merged.collected_at == NOW - 10
    assert FAKE_SECRET not in merged.error.message


def test_ingest_success_keeps_stdout_machine_readable_and_stderr_clean() -> None:
    stdin = io.BytesIO(_fixture_bytes("happy.json"))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = ingest(stdin, stdout, stderr, now=NOW)

    assert exit_code == 0
    assert stderr.getvalue() == ""
    payload = json.loads(stdout.getvalue())
    assert payload["result"] == "success"
    assert payload["collected_at"] == NOW
    assert payload["meters"][0]["remaining_percentage"] == 76.5
    assert FAKE_SECRET not in stdout.getvalue()
    assert stdout.getvalue().endswith("\n")


def test_ingest_failure_writes_typed_json_stdout_and_diagnostic_stderr() -> None:
    stdin = io.BytesIO(_fixture_bytes("malformed.json"))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = ingest(stdin, stdout, stderr, now=NOW)

    assert exit_code == 1
    payload = json.loads(stdout.getvalue())
    assert payload["result"] == "failure"
    assert payload["category"] == "malformed_response"
    assert payload["code"] == "malformed_json"
    diagnostic = stderr.getvalue()
    assert "malformed_response" in diagnostic
    assert FAKE_SECRET not in stdout.getvalue()
    assert FAKE_SECRET not in diagnostic
    assert stdout.getvalue().endswith("\n")


def test_ingest_rejects_oversized_stdin_without_parsing_it() -> None:
    stdin = io.BytesIO(_fixture_bytes("happy.json"))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = ingest(stdin, stdout, stderr, now=NOW, max_bytes=16)

    assert exit_code == 1
    payload = json.loads(stdout.getvalue())
    assert payload["code"] == "input_too_large"
    assert payload["result"] == "failure"
    assert FAKE_SECRET not in stdout.getvalue()
    assert FAKE_SECRET not in stderr.getvalue()


def test_module_cli_separates_stdout_and_stderr_channels() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "agent_meter.providers.claude"],
        input=_fixture_bytes("happy.json"),
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout.decode("utf-8"))
    assert payload["result"] == "success"
    assert payload["source"] == CLAUDE_SOURCE
    assert FrozenClock(unix_seconds=payload["collected_at"]).now() >= 0
    assert FAKE_SECRET not in completed.stdout.decode("utf-8")
    assert completed.stderr == b""

    failed = subprocess.run(
        [sys.executable, "-m", "agent_meter.providers.claude"],
        input=_fixture_bytes("null-rate-limits.json"),
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert failed.returncode == 1
    failure_payload = json.loads(failed.stdout.decode("utf-8"))
    assert failure_payload["result"] == "failure"
    assert failure_payload["code"] == "missing_rate_limits"
    assert FAKE_SECRET not in failed.stdout.decode("utf-8")
    assert FAKE_SECRET not in failed.stderr.decode("utf-8")
    assert failed.stderr.decode("utf-8").startswith("claude:")
