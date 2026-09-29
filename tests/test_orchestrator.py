"""One-shot provider orchestration tests.

Fake adapters only. Clock, filesystem, and live provider I/O are injected.
Values are fictional.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema.validators import Draft202012Validator

from agent_meter.cache import ProviderCollectionFailure, ProviderCollectionSuccess
from agent_meter.freshness import StaleSettings
from agent_meter.models import (
    ProviderError,
    ProviderOk,
    ProviderStale,
    ProviderUnavailable,
    QuotaMeter,
    UsageSnapshot,
    dump_usage_snapshot,
    parse_usage_snapshot,
)
from agent_meter.orchestrator import (
    CONFIGURED_PROVIDER_IDS,
    DEFAULT_OUTPUT_PATH,
    build_live_collectors,
    collect_snapshot,
    main,
    write_usage_snapshot,
)
from agent_meter.providers.claude import CLAUDE_PROVIDER_ID, CLAUDE_SOURCE
from agent_meter.providers.codex_rpc import CODEX_PROVIDER_ID, CODEX_SOURCE
from agent_meter.providers.cursor_rpc import CURSOR_PROVIDER_ID, CURSOR_SOURCE

NOW = 2_000_000_000
FAKE_SECRET = "PLANTED_PROVIDER_TOKEN_VALUE_DO_NOT_EMIT"
REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "usage-v0.1.schema.json"
CLAUDE_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "providers" / "claude" / "happy.json"
)


def _schema_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert isinstance(schema, dict)
    return Draft202012Validator(schema)


def _quota(*, meter_id: str, label: str, remaining_percentage: float) -> QuotaMeter:
    return QuotaMeter(
        id=meter_id,
        label=label,
        kind="quota",
        unit="percent",
        remaining_percentage=remaining_percentage,
        reset_at=NOW + 600,
    )


def _success(
    provider_id: str,
    source: str,
    *,
    remaining_percentage: float,
    meter_id: str,
    label: str,
) -> ProviderCollectionSuccess:
    return ProviderCollectionSuccess(
        provider_id=provider_id,
        source=source,
        collected_at=NOW,
        meters=(_quota(meter_id=meter_id, label=label, remaining_percentage=remaining_percentage),),
    )


def _claude_ok() -> ProviderCollectionSuccess:
    return _success(
        CLAUDE_PROVIDER_ID,
        CLAUDE_SOURCE,
        remaining_percentage=76.5,
        meter_id="five_hour",
        label="5 hour",
    )


def _codex_ok() -> ProviderCollectionSuccess:
    return _success(
        CODEX_PROVIDER_ID,
        CODEX_SOURCE,
        remaining_percentage=82,
        meter_id="five_hour",
        label="5 hour",
    )


def _cursor_ok() -> ProviderCollectionSuccess:
    return _success(
        CURSOR_PROVIDER_ID,
        CURSOR_SOURCE,
        remaining_percentage=36.55,
        meter_id="cursor_models",
        label="Cursor Models",
    )


def _constant(
    result: ProviderCollectionSuccess | ProviderCollectionFailure,
) -> Any:
    def collector(
        *, now: int, deadline_seconds: float
    ) -> ProviderCollectionSuccess | ProviderCollectionFailure:
        assert now == NOW
        assert deadline_seconds > 0
        return result

    return collector


def _raise(exc: BaseException) -> Any:
    def collector(
        *, now: int, deadline_seconds: float
    ) -> ProviderCollectionSuccess | ProviderCollectionFailure:
        del now, deadline_seconds
        raise exc

    return collector


def _timeout_failure(provider_id: str, source: str) -> ProviderCollectionFailure:
    return ProviderCollectionFailure(
        provider_id=provider_id,
        source=source,
        category="timeout",
        message="Provider collection timed out",
        retryable=True,
        code="deadline_exceeded",
    )


def _assert_schema_valid(snapshot: UsageSnapshot) -> dict[str, Any]:
    dumped = dump_usage_snapshot(snapshot)
    _schema_validator().validate(dumped)
    parsed = parse_usage_snapshot(dumped)
    assert parsed.status == snapshot.status
    return dumped


def test_all_ok_snapshot_is_schema_valid() -> None:
    stderr = io.StringIO()
    snapshot = collect_snapshot(
        {
            "claude": _constant(_claude_ok()),
            "codex": _constant(_codex_ok()),
            "cursor": _constant(_cursor_ok()),
        },
        now=NOW,
        settings=StaleSettings(),
        stderr=stderr,
    )

    dumped = _assert_schema_valid(snapshot)
    assert snapshot.status == "ok"
    assert list(snapshot.providers) == list(CONFIGURED_PROVIDER_IDS)
    for provider_id in CONFIGURED_PROVIDER_IDS:
        provider = snapshot.providers[provider_id]
        assert isinstance(provider, ProviderOk)
        assert provider.meters
        assert all(
            getattr(meter, "remaining_percentage", None) is not None for meter in provider.meters
        )
    serialized = json.dumps(dumped)
    assert FAKE_SECRET not in serialized
    assert FAKE_SECRET not in stderr.getvalue()
    assert "claude: ok" in stderr.getvalue()


def test_partial_keeps_successful_providers_when_one_times_out() -> None:
    stderr = io.StringIO()
    snapshot = collect_snapshot(
        {
            "claude": _constant(_claude_ok()),
            "codex": _constant(_timeout_failure(CODEX_PROVIDER_ID, CODEX_SOURCE)),
            "cursor": _constant(_cursor_ok()),
        },
        now=NOW,
        settings=StaleSettings(),
        stderr=stderr,
    )

    dumped = _assert_schema_valid(snapshot)
    assert snapshot.status == "partial"
    assert isinstance(snapshot.providers["claude"], ProviderOk)
    assert isinstance(snapshot.providers["cursor"], ProviderOk)
    codex = snapshot.providers["codex"]
    assert isinstance(codex, ProviderError)
    assert codex.meters == []
    assert "remaining_percentage" not in json.dumps(dumped["providers"]["codex"])
    assert FAKE_SECRET not in json.dumps(dumped)
    assert "codex: timeout:" in stderr.getvalue()


def test_all_failed_writes_error_snapshot_without_false_zeros() -> None:
    snapshot = collect_snapshot(
        {
            "claude": _constant(
                ProviderCollectionFailure(
                    provider_id=CLAUDE_PROVIDER_ID,
                    source=CLAUDE_SOURCE,
                    category="malformed_response",
                    message="Claude status-line input is empty",
                    retryable=False,
                    code="empty_input",
                )
            ),
            "codex": _constant(_timeout_failure(CODEX_PROVIDER_ID, CODEX_SOURCE)),
            "cursor": _constant(
                ProviderCollectionFailure(
                    provider_id=CURSOR_PROVIDER_ID,
                    source=CURSOR_SOURCE,
                    category="not_authenticated",
                    message="Cursor session is unavailable",
                    retryable=False,
                )
            ),
        },
        now=NOW,
        settings=StaleSettings(),
    )

    dumped = _assert_schema_valid(snapshot)
    assert snapshot.status == "error"
    cursor = snapshot.providers["cursor"]
    assert isinstance(cursor, ProviderUnavailable)
    for provider in snapshot.providers.values():
        assert provider.meters == []
    serialized = json.dumps(dumped)
    assert serialized.count('"remaining_percentage"') == 0
    assert FAKE_SECRET not in serialized


def test_adapter_throw_is_isolated_and_does_not_leak_exception_text() -> None:
    order: list[str] = []

    def claude(*, now: int, deadline_seconds: float) -> ProviderCollectionSuccess:
        del now, deadline_seconds
        order.append("claude")
        return _claude_ok()

    def codex(*, now: int, deadline_seconds: float) -> ProviderCollectionSuccess:
        del now, deadline_seconds
        order.append("codex")
        raise RuntimeError(FAKE_SECRET)

    def cursor(*, now: int, deadline_seconds: float) -> ProviderCollectionSuccess:
        del now, deadline_seconds
        order.append("cursor")
        return _cursor_ok()

    stderr = io.StringIO()
    snapshot = collect_snapshot(
        {"claude": claude, "codex": codex, "cursor": cursor},
        now=NOW,
        settings=StaleSettings(),
        stderr=stderr,
    )

    dumped = _assert_schema_valid(snapshot)
    assert order == ["claude", "codex", "cursor"]
    assert snapshot.status == "partial"
    assert isinstance(snapshot.providers["claude"], ProviderOk)
    assert isinstance(snapshot.providers["cursor"], ProviderOk)
    failed = snapshot.providers["codex"]
    assert isinstance(failed, ProviderError)
    assert failed.error.category == "internal"
    assert failed.meters == []
    diagnostic = stderr.getvalue()
    serialized = json.dumps(dumped)
    assert FAKE_SECRET not in serialized
    assert FAKE_SECRET not in diagnostic
    assert FAKE_SECRET not in failed.error.message


def test_adapter_timeout_error_is_isolated() -> None:
    stderr = io.StringIO()
    snapshot = collect_snapshot(
        {
            "claude": _constant(_claude_ok()),
            "codex": _raise(TimeoutError(FAKE_SECRET)),
            "cursor": _constant(_cursor_ok()),
        },
        now=NOW,
        settings=StaleSettings(),
        stderr=stderr,
    )

    dumped = _assert_schema_valid(snapshot)
    assert snapshot.status == "partial"
    failed = snapshot.providers["codex"]
    assert isinstance(failed, ProviderError)
    assert failed.error.category == "timeout"
    assert failed.meters == []
    assert FAKE_SECRET not in json.dumps(dumped)
    assert FAKE_SECRET not in stderr.getvalue()
    assert FAKE_SECRET not in failed.error.message


def test_malformed_adapter_output_does_not_rewrite_other_providers() -> None:
    invalid_meter = QuotaMeter.model_construct(
        id="weekly",
        label="Weekly",
        kind="quota",
        unit="percent",
        remaining_percentage=101,
    )
    malformed = ProviderCollectionSuccess(
        provider_id=CODEX_PROVIDER_ID,
        source=CODEX_SOURCE,
        collected_at=NOW,
        meters=(invalid_meter,),
    )
    snapshot = collect_snapshot(
        {
            "claude": _constant(_claude_ok()),
            "codex": _constant(malformed),
            "cursor": _constant(_cursor_ok()),
        },
        now=NOW,
        settings=StaleSettings(),
    )

    dumped = _assert_schema_valid(snapshot)
    assert snapshot.status == "partial"
    assert isinstance(snapshot.providers["claude"], ProviderOk)
    assert isinstance(snapshot.providers["cursor"], ProviderOk)
    failed = snapshot.providers["codex"]
    assert isinstance(failed, ProviderError)
    assert failed.meters == []
    assert dumped["providers"]["codex"]["error"]["category"] == "malformed_response"
    assert FAKE_SECRET not in json.dumps(dumped)


def test_typed_failure_secret_is_absent_from_stderr_snapshot_and_file(tmp_path: Path) -> None:
    stderr = io.StringIO()
    snapshot = collect_snapshot(
        {
            "claude": _constant(_claude_ok()),
            "codex": _constant(
                ProviderCollectionFailure(
                    provider_id=CODEX_PROVIDER_ID,
                    source=FAKE_SECRET,
                    category="timeout",
                    message=FAKE_SECRET,
                    retryable=True,
                    code=FAKE_SECRET,
                )
            ),
            "cursor": _constant(_cursor_ok()),
        },
        now=NOW,
        settings=StaleSettings(),
        stderr=stderr,
    )
    output = tmp_path / "usage.json"
    write_usage_snapshot(output, snapshot)

    dumped = _assert_schema_valid(snapshot)
    failed = snapshot.providers["codex"]
    assert isinstance(failed, ProviderError)
    assert failed.error.category == "timeout"
    assert failed.error.retryable is True
    assert failed.source == CODEX_SOURCE
    diagnostic = stderr.getvalue()
    serialized = json.dumps(dumped)
    file_text = output.read_text(encoding="utf-8")
    assert FAKE_SECRET not in diagnostic
    assert FAKE_SECRET not in serialized
    assert FAKE_SECRET not in file_text
    assert FAKE_SECRET not in failed.error.message
    assert getattr(failed.error, "code", None) != FAKE_SECRET
    assert "code" not in dumped["providers"]["codex"]["error"]


def test_untyped_adapter_output_becomes_isolated_failure() -> None:
    def garbage(*, now: int, deadline_seconds: float) -> object:
        del now, deadline_seconds
        return {"token": FAKE_SECRET, "remaining_percentage": 0}

    snapshot = collect_snapshot(
        {
            "claude": _constant(_claude_ok()),
            "codex": garbage,  # type: ignore[dict-item]
            "cursor": _constant(_cursor_ok()),
        },
        now=NOW,
        settings=StaleSettings(),
    )

    dumped = _assert_schema_valid(snapshot)
    failed = snapshot.providers["codex"]
    assert isinstance(failed, ProviderError)
    assert failed.error.category == "malformed_response"
    assert failed.meters == []
    serialized = json.dumps(dumped)
    assert FAKE_SECRET not in serialized
    assert serialized.count('"remaining_percentage": 0') == 0


def test_wrong_provider_id_cannot_overwrite_another_provider() -> None:
    snapshot = collect_snapshot(
        {
            "claude": _constant(_claude_ok()),
            "codex": _constant(_cursor_ok()),
            "cursor": _constant(_cursor_ok()),
        },
        now=NOW,
        settings=StaleSettings(),
    )

    dumped = _assert_schema_valid(snapshot)
    assert isinstance(snapshot.providers["cursor"], ProviderOk)
    failed = snapshot.providers["codex"]
    assert isinstance(failed, ProviderError)
    assert failed.error.category == "internal"
    assert dumped["providers"]["codex"]["source"] == CODEX_SOURCE


def test_last_good_data_survives_a_later_failure() -> None:
    previous_codex = ProviderOk(
        status="ok",
        source=CODEX_SOURCE,
        collected_at=NOW - 30,
        stale_after_seconds=900,
        meters=[_quota(meter_id="five_hour", label="5 hour", remaining_percentage=82)],
    )
    snapshot = collect_snapshot(
        {
            "claude": _constant(_claude_ok()),
            "codex": _constant(_timeout_failure(CODEX_PROVIDER_ID, CODEX_SOURCE)),
            "cursor": _constant(_cursor_ok()),
        },
        now=NOW,
        settings=StaleSettings(),
        previous={"codex": previous_codex},
    )

    dumped = _assert_schema_valid(snapshot)
    assert snapshot.status == "partial"
    stale = snapshot.providers["codex"]
    assert isinstance(stale, ProviderStale)
    assert stale.meters[0].remaining_percentage == 82
    assert stale.collected_at == NOW - 30
    assert dumped["providers"]["codex"]["meters"][0]["remaining_percentage"] == 82


def test_write_usage_snapshot_is_schema_valid_and_redacted(tmp_path: Path) -> None:
    snapshot = collect_snapshot(
        {
            "claude": _constant(_claude_ok()),
            "codex": _raise(RuntimeError(FAKE_SECRET)),
            "cursor": _constant(_cursor_ok()),
        },
        now=NOW,
        settings=StaleSettings(),
    )
    output = tmp_path / "usage.json"
    write_usage_snapshot(output, snapshot)

    payload = json.loads(output.read_text(encoding="utf-8"))
    _schema_validator().validate(payload)
    assert FAKE_SECRET not in output.read_text(encoding="utf-8")
    assert [path.name for path in tmp_path.iterdir()] == ["usage.json"]


def test_cli_without_live_does_not_write_or_collect() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "agent_meter"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"--live" in completed.stderr
    assert FAKE_SECRET not in completed.stderr.decode("utf-8")


def test_main_live_writes_snapshot_with_injected_collectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "usage.json"

    def fake_collectors(**_kwargs: object) -> dict[str, Any]:
        return {
            "claude": _constant(_claude_ok()),
            "codex": _constant(_codex_ok()),
            "cursor": _constant(_cursor_ok()),
        }

    monkeypatch.setattr("agent_meter.orchestrator.build_live_collectors", fake_collectors)
    monkeypatch.setattr("agent_meter.orchestrator._unix_now", lambda: NOW)
    assert main(["--live", "--output", str(output)]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    _schema_validator().validate(payload)
    assert payload["status"] == "ok"
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert FAKE_SECRET not in captured.out
    assert FAKE_SECRET not in captured.err
    assert DEFAULT_OUTPUT_PATH == Path("usage.json")


def test_live_collectors_use_claude_fixture_and_fake_siblings() -> None:
    collectors = build_live_collectors(
        claude_statusline=CLAUDE_FIXTURE,
        collect_codex=_constant(_codex_ok()),
        collect_cursor=_constant(_cursor_ok()),
        stdin=io.BytesIO(),
    )
    snapshot = collect_snapshot(
        collectors,
        now=NOW,
        settings=StaleSettings(),
    )
    dumped = _assert_schema_valid(snapshot)
    assert snapshot.status == "ok"
    claude = snapshot.providers["claude"]
    assert isinstance(claude, ProviderOk)
    assert claude.source == CLAUDE_SOURCE
    assert FAKE_SECRET not in json.dumps(dumped)


def test_missing_claude_statusline_is_unavailable_not_a_false_zero() -> None:
    collectors = build_live_collectors(
        claude_statusline=None,
        collect_codex=_constant(_codex_ok()),
        collect_cursor=_constant(_cursor_ok()),
        stdin=io.BytesIO(),
    )
    snapshot = collect_snapshot(collectors, now=NOW, settings=StaleSettings())
    dumped = _assert_schema_valid(snapshot)
    assert snapshot.status == "partial"
    claude = snapshot.providers["claude"]
    assert isinstance(claude, ProviderUnavailable)
    assert claude.meters == []
    assert "remaining_percentage" not in json.dumps(dumped["providers"]["claude"])
