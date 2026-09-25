"""Cursor period-usage adapter tests.

Clock, the real Cursor login state, and live Connect RPC are never used.
Values are fictional. The transport tests inject a temporary state directory.
"""

from __future__ import annotations

import email.message
import json
import math
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

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
    collect,
    collect_period_usage,
    main,
)
from agent_meter.providers.cursor_rpc import (
    CursorTransport,
    PreparedRequest,
    TransportResponse,
    default_state_dir,
    urllib_transport,
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


def test_unsafe_spend_cents_are_omitted_without_raising() -> None:
    unsafe_cents = 10**400 + 1

    used = collect_period_usage(
        {
            "planUsage": {"autoPercentUsed": 10, "apiPercentUsed": 20},
            "spendLimitUsage": {"individualUsed": unsafe_cents},
        },
        now=NOW,
    )
    assert isinstance(used, ProviderCollectionSuccess)
    assert [meter.id for meter in used.meters] == ["cursor_models", "other_models"]

    limit = collect_period_usage(
        {
            "planUsage": {"autoPercentUsed": 10},
            "spendLimitUsage": {"individualUsed": 450, "individualLimit": unsafe_cents},
        },
        now=NOW,
    )
    assert isinstance(limit, ProviderCollectionSuccess)
    limited = _spend(limit)
    assert limited.used == 4.5
    assert "limit" not in limited.model_dump(exclude_unset=True)

    remaining = collect_period_usage(
        {
            "planUsage": {"autoPercentUsed": 10},
            "spendLimitUsage": {
                "individualUsed": 450,
                "individualRemaining": unsafe_cents,
            },
        },
        now=NOW,
    )
    assert isinstance(remaining, ProviderCollectionSuccess)
    without_remaining = _spend(remaining)
    assert without_remaining.used == 4.5
    assert "remaining" not in without_remaining.model_dump(exclude_unset=True)


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


def _write_state(root: Path, *, token: str | None, quoted: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    database = root / "state.vscdb"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    if token is not None:
        stored = json.dumps(token) if quoted else token
        connection.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            ("cursorAuth/accessToken", stored),
        )
    connection.commit()
    connection.close()
    (root / "storage.json").write_text(
        json.dumps(
            {
                "telemetry.machineId": "machine-a",
                "telemetry.macMachineId": "mac-a",
            }
        ),
        encoding="utf-8",
    )
    return database


def _accepting_transport(body: bytes) -> CursorTransport:
    def transport(
        request: PreparedRequest, *, deadline_seconds: float
    ) -> TransportResponse | ProviderCollectionFailure:
        headers = dict(request.headers)
        constructed = (
            headers.get("Authorization") == f"Bearer {FAKE_SECRET}"
            and headers.get("x-cursor-checksum") == "00000000machine-a/mac-a"
            and headers.get("x-cursor-client-version") == "9.9.9"
            and headers.get("x-cursor-client-type") == "ide"
            and request.body == b"{}"
            and request.url.endswith("/GetCurrentPeriodUsage")
            and deadline_seconds == 3
        )
        if not constructed:
            return ProviderCollectionFailure(
                provider_id=CURSOR_PROVIDER_ID,
                source=CURSOR_SOURCE,
                category="upstream",
                message="request was not constructed as expected",
                retryable=False,
                code="bad_request",
            )
        return TransportResponse(status=200, body=body)

    return transport


def test_collect_maps_mocked_connect_rpc_without_leaking_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _write_state(tmp_path, token=FAKE_SECRET)
    before = database.read_bytes()
    storage_before = (tmp_path / "storage.json").read_bytes()

    def forbid_real_state() -> Path:
        raise AssertionError("real Cursor state dir")

    monkeypatch.setattr(
        "agent_meter.providers.cursor_rpc.default_state_dir",
        forbid_real_state,
    )
    result = collect(
        now=NOW,
        deadline_seconds=3,
        state_dir=tmp_path,
        client_version="9.9.9",
        transport=_accepting_transport((FIXTURE_DIR / "happy.json").read_bytes()),
    )

    assert isinstance(result, ProviderCollectionSuccess)
    assert [meter.id for meter in result.meters] == [
        "cursor_models",
        "other_models",
        "on_demand",
    ]
    dumped = json.dumps(dump_collection_result(result))
    assert FAKE_SECRET not in dumped
    assert "machine-a" not in dumped
    assert "mac-a" not in dumped
    assert database.read_bytes() == before
    assert (tmp_path / "storage.json").read_bytes() == storage_before


def test_json_encoded_token_is_accepted(tmp_path: Path) -> None:
    _write_state(tmp_path, token=FAKE_SECRET, quoted=True)
    result = collect(
        now=NOW,
        deadline_seconds=3,
        state_dir=tmp_path,
        client_version="9.9.9",
        transport=_accepting_transport(b'{"planUsage":{"autoPercentUsed":1}}'),
    )
    assert isinstance(result, ProviderCollectionSuccess)
    assert FAKE_SECRET not in json.dumps(dump_collection_result(result))


def test_missing_local_state_is_not_installed(tmp_path: Path) -> None:
    result = collect(
        now=NOW,
        state_dir=tmp_path,
        client_version="9.9.9",
        transport=_accepting_transport(b"{}"),
    )
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "not_installed"
    assert result.code == "missing_local_state"
    assert FAKE_SECRET not in result.message


def test_database_without_login_is_not_authenticated(tmp_path: Path) -> None:
    _write_state(tmp_path, token=None)
    result = collect(
        now=NOW,
        state_dir=tmp_path,
        client_version="9.9.9",
        transport=_accepting_transport(b"{}"),
    )
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "not_authenticated"
    assert result.code == "missing_login"
    assert FAKE_SECRET not in result.message


def test_http_401_is_auth_expired_without_reading_body(tmp_path: Path) -> None:
    _write_state(tmp_path, token=FAKE_SECRET)

    def transport(request: PreparedRequest, *, deadline_seconds: float) -> TransportResponse:
        del request, deadline_seconds
        return TransportResponse(status=401, body=FAKE_SECRET.encode("utf-8"))

    result = collect(
        now=NOW,
        state_dir=tmp_path,
        client_version="9.9.9",
        transport=transport,
    )
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "auth_expired"
    assert result.retryable is False
    assert FAKE_SECRET not in result.message


def test_server_error_is_retryable_upstream(tmp_path: Path) -> None:
    _write_state(tmp_path, token=FAKE_SECRET)

    def transport(request: PreparedRequest, *, deadline_seconds: float) -> TransportResponse:
        del request, deadline_seconds
        return TransportResponse(status=503, body=FAKE_SECRET.encode("utf-8"))

    result = collect(
        now=NOW,
        state_dir=tmp_path,
        client_version="9.9.9",
        transport=transport,
    )
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "upstream"
    assert result.retryable is True
    assert FAKE_SECRET not in result.message


def test_malformed_response_body_does_not_echo_payload(tmp_path: Path) -> None:
    _write_state(tmp_path, token=FAKE_SECRET)

    def transport(request: PreparedRequest, *, deadline_seconds: float) -> TransportResponse:
        del request, deadline_seconds
        return TransportResponse(status=200, body=f"not-json {FAKE_SECRET}".encode())

    result = collect(
        now=NOW,
        state_dir=tmp_path,
        client_version="9.9.9",
        transport=transport,
    )
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "malformed_response"
    assert FAKE_SECRET not in result.message


def test_non_positive_deadline_is_timeout_without_a_request() -> None:
    request = PreparedRequest(url="https://example.invalid", body=b"{}", headers=())
    result = urllib_transport(request, deadline_seconds=0)
    assert isinstance(result, ProviderCollectionFailure)
    assert result.category == "timeout"
    assert result.retryable is True
    assert result.code == "deadline_exceeded"


def test_unsafe_json_becomes_sanitized_failure(tmp_path: Path) -> None:
    secret = FAKE_SECRET.encode("utf-8")
    bodies = [
        b'{"email":"' + secret + b'","n":' + b"1" * 5000 + b"}",
        b'{"email":"' + secret + b'","a":' + (b'{"a":' * 10000) + b"1" + (b"}" * 10000) + b"}",
        b'{"email":"' + secret + b'","planUsage":{"autoPercentUsed":NaN}}',
        b'{"email":"' + secret + b'","planUsage":{"autoPercentUsed":Infinity}}',
    ]
    _write_state(tmp_path, token=FAKE_SECRET)
    for body in bodies:

        def transport(
            request: PreparedRequest, *, deadline_seconds: float, payload: bytes = body
        ) -> TransportResponse:
            del request, deadline_seconds
            return TransportResponse(status=200, body=payload)

        result = collect(
            now=NOW,
            state_dir=tmp_path,
            client_version="9.9.9",
            transport=transport,
        )
        assert isinstance(result, ProviderCollectionFailure)
        assert result.category == "malformed_response"
        assert result.code == "malformed_json"
        assert FAKE_SECRET not in result.message

    for name, code in (
        ("storage.json", "unreadable_local_state"),
        ("package.json", "missing_client_version"),
    ):
        for body in bodies:
            root = tmp_path / "cases" / name / str(len(body))
            _write_state(root, token=FAKE_SECRET)
            if name == "storage.json":
                (root / "storage.json").write_bytes(body)
                result = collect(
                    now=NOW,
                    state_dir=root,
                    client_version="9.9.9",
                    transport=_accepting_transport(b'{"planUsage":{"autoPercentUsed":1}}'),
                )
            else:
                version_path = root / "package.json"
                version_path.write_bytes(body)
                result = collect(
                    now=NOW,
                    state_dir=root,
                    version_path=version_path,
                    transport=_accepting_transport(b'{"planUsage":{"autoPercentUsed":1}}'),
                )
            assert isinstance(result, ProviderCollectionFailure)
            assert result.category == "not_installed"
            assert result.code == code
            assert FAKE_SECRET not in result.message


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [(401, "auth_expired", False), (403, "upstream", False), (503, "upstream", True)],
)
def test_urllib_and_injected_transport_share_http_status_policy(
    status: int,
    category: str,
    retryable: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_state(tmp_path, token=FAKE_SECRET)

    def transport(request: PreparedRequest, *, deadline_seconds: float) -> TransportResponse:
        del request, deadline_seconds
        return TransportResponse(status=status, body=FAKE_SECRET.encode("utf-8"))

    injected = collect(
        now=NOW,
        state_dir=tmp_path,
        client_version="9.9.9",
        transport=transport,
    )

    class _RaisingOpener:
        def open(self, request: urllib.request.Request, timeout: float | None = None) -> object:
            del timeout
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                FAKE_SECRET,
                email.message.Message(),
                None,
            )

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: _RaisingOpener(),
    )
    produced = urllib_transport(
        PreparedRequest(
            url="https://example.invalid/GetCurrentPeriodUsage", body=b"{}", headers=()
        ),
        deadline_seconds=1,
    )

    assert isinstance(injected, ProviderCollectionFailure)
    assert isinstance(produced, ProviderCollectionFailure)
    assert injected.category == produced.category == category
    assert injected.retryable is produced.retryable is retryable
    assert FAKE_SECRET not in injected.message
    assert FAKE_SECRET not in produced.message


def test_cli_without_live_does_not_read_cursor_state() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "agent_meter.providers.cursor"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"--live" in completed.stderr
    assert FAKE_SECRET not in completed.stderr.decode("utf-8")
    assert main([]) == 2


def test_default_state_dir_points_at_cursor_global_storage() -> None:
    assert default_state_dir().name == "globalStorage"
