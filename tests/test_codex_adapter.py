"""Codex app-server adapter tests.

Clock, live Codex sessions, and the real `codex` binary are never used.
Values and JSON-RPC envelopes are fictional.
"""

from __future__ import annotations

import io
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import TextIO

import pytest

from agent_meter.cache import (
    ProviderCollectionFailure,
    ProviderCollectionSuccess,
    apply_collection_result,
)
from agent_meter.freshness import StaleSettings
from agent_meter.models import QuotaMeter
from agent_meter.providers.codex import (
    collect,
    collect_rate_limits,
    main,
)
from agent_meter.providers.codex_rpc import CODEX_PROVIDER_ID, CODEX_SOURCE
from agent_meter.providers.result_dump import dump_collection_result

NOW = 2_000_000_000
FAKE_SECRET = "PLANTED_PROVIDER_TOKEN_VALUE_DO_NOT_EMIT"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "providers" / "codex"
FAKE_SERVER = FIXTURE_DIR / "fake_app_server.py"


def _fixture(name: str) -> object:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _meter_by_id(result: ProviderCollectionSuccess, meter_id: str) -> QuotaMeter:
    matched = [meter for meter in result.meters if meter.id == meter_id]
    assert len(matched) == 1
    meter = matched[0]
    assert isinstance(meter, QuotaMeter)
    return meter


def _command(*args: str) -> list[str]:
    return [sys.executable, str(FAKE_SERVER), *args]


def _collect_scenario(
    scenario: str,
    *,
    deadline_seconds: float = 3.0,
    pid_file: Path | None = None,
    stderr: TextIO | None = None,
) -> ProviderCollectionSuccess | ProviderCollectionFailure:
    command = _command(scenario)
    if pid_file is not None:
        command.extend(["--pid-file", str(pid_file)])
    return collect(
        now=NOW,
        deadline_seconds=deadline_seconds,
        command=command,
        env={},
        stderr=stderr,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_happy_rate_limits_map_used_percent_and_ignore_extras() -> None:
    result = collect_rate_limits(_fixture("happy.json"), now=NOW)

    assert isinstance(result, ProviderCollectionSuccess)
    assert result.provider_id == CODEX_PROVIDER_ID
    assert result.source == CODEX_SOURCE
    assert result.collected_at == NOW
    assert [meter.id for meter in result.meters] == ["five_hour", "weekly"]

    five_hour = _meter_by_id(result, "five_hour")
    assert five_hour.label == "5 hour"
    assert five_hour.kind == "quota"
    assert five_hour.unit == "percent"
    assert five_hour.remaining_percentage == 82
    assert five_hour.reset_at == 2_000_004_000

    weekly = _meter_by_id(result, "weekly")
    assert weekly.label == "Weekly"
    assert weekly.remaining_percentage == 95
    assert weekly.reset_at == 2_000_600_400

    dumped = dump_collection_result(result)
    serialized = json.dumps(dumped)
    assert FAKE_SECRET not in serialized
    assert "rateLimitsByLimitId" not in dumped
    assert "account" not in dumped
    assert dumped["result"] == "success"


def test_independently_absent_window_still_succeeds_with_one_meter() -> None:
    result = collect_rate_limits(_fixture("one-window.json"), now=NOW)

    assert isinstance(result, ProviderCollectionSuccess)
    assert [meter.id for meter in result.meters] == ["five_hour"]
    assert _meter_by_id(result, "five_hour").remaining_percentage == 90


def test_missing_or_null_resets_at_keeps_meter_without_reset() -> None:
    result = collect_rate_limits(_fixture("missing-reset.json"), now=NOW)

    assert isinstance(result, ProviderCollectionSuccess)
    five_hour = _meter_by_id(result, "five_hour")
    weekly = _meter_by_id(result, "weekly")
    assert five_hour.remaining_percentage == 60
    assert weekly.remaining_percentage == 85
    assert "reset_at" not in five_hour.model_dump(exclude_unset=True)
    assert "reset_at" not in weekly.model_dump(exclude_unset=True)


def test_mismatched_window_duration_skips_that_window() -> None:
    result = collect_rate_limits(_fixture("duration-mismatch.json"), now=NOW)

    assert isinstance(result, ProviderCollectionSuccess)
    assert [meter.id for meter in result.meters] == ["weekly"]
    assert _meter_by_id(result, "weekly").remaining_percentage == 92


def test_missing_window_duration_still_maps_by_primary_secondary() -> None:
    result = collect_rate_limits(
        {"rateLimits": {"primary": {"usedPercent": 25}, "secondary": {"usedPercent": 40}}},
        now=NOW,
    )
    assert isinstance(result, ProviderCollectionSuccess)
    assert _meter_by_id(result, "five_hour").remaining_percentage == 75
    assert _meter_by_id(result, "weekly").remaining_percentage == 60


def test_integer_valued_reset_float_normalizes_to_unix_seconds() -> None:
    result = collect_rate_limits(
        {"rateLimits": {"primary": {"usedPercent": 1, "resetsAt": 2000004000.0}}},
        now=NOW,
    )
    assert isinstance(result, ProviderCollectionSuccess)
    assert _meter_by_id(result, "five_hour").reset_at == 2_000_004_000


def test_millisecond_or_invalid_resets_at_is_omitted() -> None:
    result = collect_rate_limits(
        {
            "rateLimits": {
                "primary": {"usedPercent": 20, "resetsAt": 1_788_256_831_000},
                "secondary": {"usedPercent": 30, "resetsAt": True},
            }
        },
        now=NOW,
    )
    assert isinstance(result, ProviderCollectionSuccess)
    assert "reset_at" not in _meter_by_id(result, "five_hour").model_dump(exclude_unset=True)
    assert "reset_at" not in _meter_by_id(result, "weekly").model_dump(exclude_unset=True)


def test_percentage_boundaries_zero_and_full_are_valid() -> None:
    result = collect_rate_limits(
        {
            "rateLimits": {
                "primary": {"usedPercent": 0, "resetsAt": NOW + 1},
                "secondary": {"usedPercent": 100, "resetsAt": NOW + 2},
            }
        },
        now=NOW,
    )
    assert isinstance(result, ProviderCollectionSuccess)
    assert _meter_by_id(result, "five_hour").remaining_percentage == 100
    assert _meter_by_id(result, "weekly").remaining_percentage == 0


def test_out_of_range_percentage_skips_that_window() -> None:
    result = collect_rate_limits(
        {
            "rateLimits": {
                "primary": {"usedPercent": 101},
                "secondary": {"usedPercent": 12},
            }
        },
        now=NOW,
    )
    assert isinstance(result, ProviderCollectionSuccess)
    assert [meter.id for meter in result.meters] == ["weekly"]
    assert _meter_by_id(result, "weekly").remaining_percentage == 88


def test_boolean_nan_and_string_percentages_are_not_coerced() -> None:
    for used in (True, False, "18", math.nan, math.inf, -1, None, 100.1):
        result = collect_rate_limits(
            {"rateLimits": {"primary": {"usedPercent": used}}},
            now=NOW,
        )
        assert isinstance(result, ProviderCollectionFailure)
        assert result.category == "malformed_response"
        assert result.code == "no_valid_meters"
        assert result.retryable is False
        assert FAKE_SECRET not in result.message


def test_empty_malformed_and_missing_rate_limits_fail_closed() -> None:
    cases = [
        ([], "not_object"),
        ("codex", "not_object"),
        ({}, "missing_rate_limits"),
        ({"rateLimits": None}, "missing_rate_limits"),
        ({"rateLimits": []}, "no_valid_meters"),
        ({"rateLimits": {}}, "no_valid_meters"),
        ({"rateLimits": {"primary": None, "secondary": {}}}, "no_valid_meters"),
    ]
    for payload, code in cases:
        result = collect_rate_limits(payload, now=NOW)
        assert isinstance(result, ProviderCollectionFailure), code
        assert result.category == "malformed_response"
        assert result.code == code
        assert result.provider_id == CODEX_PROVIDER_ID
        assert result.source == CODEX_SOURCE
        assert FAKE_SECRET not in result.message
        assert "\n" not in result.message


def test_happy_fake_app_server_handshake_maps_meters() -> None:
    result = _collect_scenario("happy")
    assert isinstance(result, ProviderCollectionSuccess)
    assert [meter.id for meter in result.meters] == ["five_hour", "weekly"]
    assert _meter_by_id(result, "five_hour").remaining_percentage == 82
    dumped = json.dumps(dump_collection_result(result))
    assert FAKE_SECRET not in dumped


def test_out_of_order_notifications_are_ignored_without_leaking_params() -> None:
    stderr = io.StringIO()
    result = _collect_scenario("notification_first", stderr=stderr)
    assert isinstance(result, ProviderCollectionSuccess)
    assert _meter_by_id(result, "five_hour").remaining_percentage == 82
    assert FAKE_SECRET not in result.source
    assert FAKE_SECRET not in json.dumps(dump_collection_result(result))
    assert FAKE_SECRET not in stderr.getvalue()
    assert "codex: ignored JSON-RPC" in stderr.getvalue()


def test_response_is_paired_by_request_id_not_first_line() -> None:
    result = _collect_scenario("wrong_id_first")
    assert isinstance(result, ProviderCollectionSuccess)
    assert FAKE_SECRET not in json.dumps(dump_collection_result(result))


def test_timeout_reaps_child_process(tmp_path: Path) -> None:
    pid_file = tmp_path / "timeout.pid"
    result = _collect_scenario("timeout", deadline_seconds=0.3, pid_file=pid_file)
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "timeout"
    assert result.retryable is True
    assert FAKE_SECRET not in result.message
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert not _pid_alive(pid)


def test_ignored_sigterm_is_escalated_to_sigkill(tmp_path: Path) -> None:
    pid_file = tmp_path / "ignore-term.pid"
    result = _collect_scenario("ignore_term", deadline_seconds=0.3, pid_file=pid_file)
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "timeout"
    pid = int(pid_file.read_text(encoding="utf-8"))
    assert not _pid_alive(pid)


def test_eof_after_initialize_is_malformed_without_raw_payload() -> None:
    result = _collect_scenario("eof_after_initialize")
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "malformed_response"
    assert result.code == "eof"
    assert FAKE_SECRET not in result.message


def test_non_json_stdout_line_is_malformed_without_leaking_secret() -> None:
    result = _collect_scenario("non_json")
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "malformed_response"
    assert result.code == "malformed_json"
    assert FAKE_SECRET not in result.message
    assert FAKE_SECRET not in json.dumps(dump_collection_result(result))


def test_missing_jsonrpc_result_is_malformed() -> None:
    result = _collect_scenario("missing_result")
    assert isinstance(result, ProviderCollectionFailure)
    assert result.code == "missing_result"
    assert result.category == "malformed_response"


def test_jsonrpc_error_is_upstream_without_copying_message() -> None:
    result = _collect_scenario("jsonrpc_error")
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "upstream"
    assert result.retryable is True
    assert FAKE_SECRET not in result.message
    assert "backend failed" not in result.message


def test_unauthenticated_jsonrpc_error_maps_without_raw_message() -> None:
    result = _collect_scenario("unauthenticated")
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "not_authenticated"
    assert result.retryable is False
    assert FAKE_SECRET not in result.message
    assert "UNAUTHENTICATED" not in result.message


def test_process_exit_before_result_is_upstream() -> None:
    result = _collect_scenario("exit_1")
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "upstream"
    assert result.code == "process_exited"
    assert FAKE_SECRET not in result.message


def test_missing_binary_is_not_installed() -> None:
    result = collect(
        now=NOW,
        command=["/nonexistent/agent-meter-codex-binary"],
        env={},
        deadline_seconds=1.0,
    )
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "not_installed"
    assert result.retryable is False


def test_child_env_does_not_inherit_planted_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FAKE_SECRET, FAKE_SECRET)
    result = collect(
        now=NOW,
        command=_command("fail_if_secret_in_env"),
        deadline_seconds=3.0,
    )
    assert isinstance(result, ProviderCollectionSuccess)
    assert FAKE_SECRET not in json.dumps(dump_collection_result(result))


def test_failure_lets_orchestrator_keep_last_good_snapshot() -> None:
    previous_success = collect_rate_limits(_fixture("happy.json"), now=NOW - 10)
    assert isinstance(previous_success, ProviderCollectionSuccess)
    previous = apply_collection_result(None, previous_success, settings=StaleSettings())

    failure = collect_rate_limits({"rateLimits": {}}, now=NOW)
    assert isinstance(failure, ProviderCollectionFailure)
    merged = apply_collection_result(previous, failure, settings=StaleSettings())

    assert merged.status == "stale"
    assert [meter.id for meter in merged.meters] == ["five_hour", "weekly"]
    assert merged.collected_at == NOW - 10
    assert FAKE_SECRET not in merged.error.message


def test_cli_without_live_does_not_spawn_app_server() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "agent_meter.providers.codex"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"--live" in completed.stderr
    assert FAKE_SECRET not in completed.stderr.decode("utf-8")


def test_module_cli_help_stays_off_the_protocol_channel() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "agent_meter.providers.codex", "--help"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0
    assert b"--live" in completed.stdout
    assert FAKE_SECRET not in completed.stdout.decode("utf-8")


def test_main_without_live_matches_cli_guard() -> None:
    assert main([]) == 2
