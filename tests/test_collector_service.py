"""Resident Collector lifecycle tests. Clock, network, and live providers are fake."""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_meter.cache import ProviderCollectionFailure, ProviderCollectionSuccess
from agent_meter.cache_store import SnapshotCacheStore
from agent_meter.collector_service import CollectorService
from agent_meter.instance_lock import ServiceAlreadyRunningError, ServiceInstanceLock
from agent_meter.models import (
    QuotaMeter,
    UsageSnapshot,
    dump_usage_snapshot,
    parse_usage_snapshot,
)
from agent_meter.paths import resolve_runtime_paths
from agent_meter.provider_registry import LazyLiveCollectorFactory, ProviderCollectorFactory
from agent_meter.providers.claude import CLAUDE_PROVIDER_ID, CLAUDE_SOURCE
from agent_meter.providers.codex_rpc import CODEX_PROVIDER_ID, CODEX_SOURCE
from agent_meter.providers.cursor_rpc import CURSOR_PROVIDER_ID, CURSOR_SOURCE
from agent_meter.service_models import ErrorEnvelope
from agent_meter.service_policy import (
    POLL_INTERVAL_SECONDS,
    POLL_TIMEOUT_SECONDS,
    RETRY_BACKOFF_CAP_SECONDS,
)
from agent_meter.settings import EnabledProvider, SettingsWriteRequest
from agent_meter.settings_store import SettingsSaveError, SettingsStore

NOW = 2_000_000_000
FAKE_SECRET = "PLANTED_PROVIDER_TOKEN_VALUE_DO_NOT_EMIT"
FIXTURE_SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "contracts" / "ok.json"


class FakeClocks:
    """Wall + monotonic + wakeup driven by tests. wait() never uses time.sleep."""

    def __init__(self, wall: int = NOW) -> None:
        self._wall = wall
        self._mono = 0.0
        self._cond = threading.Condition()
        self._epoch = 0
        self._pending = False

    def now(self) -> int:
        with self._cond:
            return self._wall

    def monotonic(self) -> float:
        with self._cond:
            return self._mono

    def wait(self, timeout: float | None) -> None:
        with self._cond:
            if self._pending:
                self._pending = False
                return
            epoch = self._epoch
            if timeout is None:
                while self._epoch == epoch and not self._pending:
                    self._cond.wait()
                self._pending = False
                return
            deadline = self._mono + timeout
            while self._mono < deadline and self._epoch == epoch and not self._pending:
                self._cond.wait()
            self._pending = False

    def notify(self) -> None:
        with self._cond:
            self._pending = True
            self._epoch += 1
            self._cond.notify_all()

    def advance(self, seconds: float) -> None:
        with self._cond:
            self._mono += seconds
            self._wall += int(seconds)
            self._cond.notify_all()


class SpyFactory(ProviderCollectorFactory):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.collector_for_calls: list[str] = []
        self.collect_calls: list[tuple[str, int, float]] = []
        self._results: dict[str, Any] = {}
        self._blocks: dict[str, threading.Event] = {}
        self.entered: dict[str, threading.Event] = {}

    def set_result(self, provider_id: str, result: object) -> None:
        self._results[provider_id] = result

    def block(self, provider_id: str) -> threading.Event:
        event = threading.Event()
        self._blocks[provider_id] = event
        return event

    def collector_for(self, provider_id: EnabledProvider) -> Any:
        with self._lock:
            self.collector_for_calls.append(provider_id)
        entered = self.entered.setdefault(provider_id, threading.Event())

        def collect(
            *, now: int, deadline_seconds: float
        ) -> ProviderCollectionSuccess | ProviderCollectionFailure:
            entered.set()
            gate = self._blocks.get(provider_id)
            if gate is not None:
                gate.wait(timeout=30)
            with self._lock:
                self.collect_calls.append((provider_id, now, deadline_seconds))
            result = self._results[provider_id]
            if isinstance(result, BaseException):
                raise result
            if callable(result) and not isinstance(
                result, (ProviderCollectionSuccess, ProviderCollectionFailure)
            ):
                return result(now=now, deadline_seconds=deadline_seconds)  # type: ignore[no-any-return]
            return result  # type: ignore[no-any-return]

        return collect


def _wait_until(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def _paths(tmp_path: Path) -> Any:
    return resolve_runtime_paths(config_dir=tmp_path / "config", cache_dir=tmp_path / "cache")


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
    collected_at: int = NOW,
) -> ProviderCollectionSuccess:
    return ProviderCollectionSuccess(
        provider_id=provider_id,
        source=source,
        collected_at=collected_at,
        meters=(_quota(meter_id=meter_id, label=label, remaining_percentage=remaining_percentage),),
    )


def _codex_ok(*, collected_at: int = NOW) -> ProviderCollectionSuccess:
    return _success(
        CODEX_PROVIDER_ID,
        CODEX_SOURCE,
        remaining_percentage=82,
        meter_id="five_hour",
        label="5 hour",
        collected_at=collected_at,
    )


def _cursor_ok(*, collected_at: int = NOW) -> ProviderCollectionSuccess:
    return _success(
        CURSOR_PROVIDER_ID,
        CURSOR_SOURCE,
        remaining_percentage=36.55,
        meter_id="cursor_models",
        label="Cursor Models",
        collected_at=collected_at,
    )


def _claude_ok(*, collected_at: int = NOW) -> ProviderCollectionSuccess:
    return _success(
        CLAUDE_PROVIDER_ID,
        CLAUDE_SOURCE,
        remaining_percentage=76.5,
        meter_id="five_hour",
        label="5 hour",
        collected_at=collected_at,
    )


def _auth_missing(provider_id: str, source: str) -> ProviderCollectionFailure:
    return ProviderCollectionFailure(
        provider_id=provider_id,
        source=source,
        category="not_authenticated",
        message="Provider is not authenticated",
        retryable=False,
    )


def _timeout_failure(provider_id: str, source: str) -> ProviderCollectionFailure:
    return ProviderCollectionFailure(
        provider_id=provider_id,
        source=source,
        category="timeout",
        message="Provider collection timed out",
        retryable=True,
    )


def _save_settings(paths: Any, *providers: EnabledProvider, expected_revision: int = 0) -> Any:
    return SettingsStore(paths).save(
        SettingsWriteRequest(expected_revision=expected_revision, enabled_providers=list(providers))
    )


def _fixture_snapshot() -> UsageSnapshot:
    return parse_usage_snapshot(json.loads(FIXTURE_SNAPSHOT.read_text(encoding="utf-8")))


def _make_service(
    tmp_path: Path,
    factory: SpyFactory,
    clocks: FakeClocks,
    *,
    shutdown_deadline_seconds: float = 0.5,
) -> tuple[CollectorService, Any, io.StringIO]:
    paths = _paths(tmp_path)
    logs = io.StringIO()
    service = CollectorService(
        paths,
        wall_clock=clocks,
        monotonic_clock=clocks,
        wakeup=clocks,
        collector_factory=factory,
        stderr=logs,
        shutdown_deadline_seconds=shutdown_deadline_seconds,
    )
    return service, paths, logs


def _wait_collects(factory: SpyFactory, count: int) -> None:
    _wait_until(lambda: len(factory.collect_calls) >= count)


def _wait_usage_snapshot(service: CollectorService) -> UsageSnapshot:
    def _ready() -> bool:
        return isinstance(service.usage(), UsageSnapshot)

    _wait_until(_ready)
    view = service.usage()
    assert isinstance(view, UsageSnapshot)
    return view


def test_empty_enablement_starts_idle_with_zero_live_calls(tmp_path: Path) -> None:
    factory = SpyFactory()
    clocks = FakeClocks()
    service, _paths_obj, logs = _make_service(tmp_path, factory, clocks)
    service.start()
    try:
        health = service.health()
        assert health.state == "idle"
        view = service.usage()
        assert isinstance(view, ErrorEnvelope)
        assert view.error.code == "no_enabled_providers"
        assert factory.collector_for_calls == []
        assert factory.collect_calls == []
        assert FAKE_SECRET not in logs.getvalue()
    finally:
        service.stop()
    assert factory.collect_calls == []


def test_only_codex_enabled_does_not_call_cursor_or_claude(tmp_path: Path) -> None:
    factory = SpyFactory()
    factory.set_result("codex", _codex_ok())
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    _save_settings(paths, "codex")
    service.start()
    try:
        snapshot = _wait_usage_snapshot(service)
        assert set(snapshot.providers) == {"codex"}
        assert factory.collector_for_calls == ["codex"]
        assert [item[0] for item in factory.collect_calls] == ["codex"]
        assert all(item[2] == POLL_TIMEOUT_SECONDS for item in factory.collect_calls)
        usage_again = service.usage()
        assert isinstance(usage_again, UsageSnapshot)
        assert len(factory.collect_calls) == 1
    finally:
        service.stop()


def test_interval_deadline_backoff_and_no_backlog(tmp_path: Path) -> None:
    factory = SpyFactory()
    factory.set_result("codex", _timeout_failure("codex", CODEX_SOURCE))
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    _save_settings(paths, "codex")
    service.start()
    try:
        _wait_collects(factory, 1)
        _wait_usage_snapshot(service)
        clocks.advance(POLL_INTERVAL_SECONDS - 1)
        time.sleep(0.05)
        assert len(factory.collect_calls) == 1
        clocks.advance(1)
        _wait_collects(factory, 2)
        _wait_usage_snapshot(service)
        clocks.advance(POLL_INTERVAL_SECONDS)
        time.sleep(0.05)
        assert len(factory.collect_calls) == 2
        clocks.advance(POLL_INTERVAL_SECONDS)
        _wait_collects(factory, 3)
        _wait_usage_snapshot(service)
        factory.set_result("codex", _codex_ok(collected_at=clocks.now()))
        clocks.advance(RETRY_BACKOFF_CAP_SECONDS)
        _wait_collects(factory, 4)
        _wait_usage_snapshot(service)
        clocks.advance(POLL_INTERVAL_SECONDS - 1)
        time.sleep(0.05)
        assert len(factory.collect_calls) == 4
        clocks.advance(1)
        _wait_collects(factory, 5)
    finally:
        service.stop()


def test_stuck_provider_does_not_block_the_other(tmp_path: Path) -> None:
    factory = SpyFactory()
    cursor_gate = factory.block("cursor")
    factory.set_result("codex", lambda *, now, deadline_seconds: _codex_ok(collected_at=now))
    factory.set_result("cursor", _cursor_ok())
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    SnapshotCacheStore(paths).save(_fixture_snapshot())
    _save_settings(paths, "codex", "cursor")
    service.start()
    try:
        _wait_until(
            lambda: factory.entered.get("cursor") is not None and factory.entered["cursor"].is_set()
        )
        _wait_collects(factory, 1)
        first_codex = [item for item in factory.collect_calls if item[0] == "codex"]
        assert len(first_codex) == 1
        clocks.advance(POLL_INTERVAL_SECONDS)
        _wait_until(
            lambda: len([item for item in factory.collect_calls if item[0] == "codex"]) == 2
        )
        assert len([item for item in factory.collect_calls if item[0] == "cursor"]) == 0
        snapshot = service.usage()
        assert isinstance(snapshot, UsageSnapshot)
        assert snapshot.providers["codex"].collected_at == NOW + POLL_INTERVAL_SECONDS
        assert snapshot.providers["cursor"].collected_at == 1_999_999_970
    finally:
        cursor_gate.set()
        service.stop()


def test_no_overlap_when_previous_job_still_running(tmp_path: Path) -> None:
    factory = SpyFactory()
    gate = factory.block("codex")
    factory.set_result("codex", _codex_ok())
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    _save_settings(paths, "codex")
    service.start()
    try:
        _wait_until(
            lambda: factory.entered.get("codex") is not None and factory.entered["codex"].is_set()
        )
        clocks.advance(POLL_INTERVAL_SECONDS * 3)
        time.sleep(0.05)
        assert len(factory.collect_calls) == 0
        gate.set()
        _wait_collects(factory, 1)
        _wait_until(lambda: len(factory.collect_calls) >= 1)
        time.sleep(0.05)
        # Missed ticks collapse to at most one catch-up collect, never a backlog.
        assert len(factory.collect_calls) <= 2
    finally:
        gate.set()
        service.stop()


def test_disable_during_collection_discards_late_success_and_failure(tmp_path: Path) -> None:
    factory = SpyFactory()
    gate = factory.block("codex")
    factory.set_result("codex", _codex_ok())
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    _save_settings(paths, "codex")
    service.start()
    try:
        _wait_until(
            lambda: factory.entered.get("codex") is not None and factory.entered["codex"].is_set()
        )
        view = service.usage()
        assert isinstance(view, ErrorEnvelope)
        assert view.error.code == "snapshot_not_ready"
        saved = service.apply_settings(
            SettingsWriteRequest(expected_revision=1, enabled_providers=[])
        )
        assert saved.enabled_providers == []
        assert service.health().state == "idle"
        gate.set()
        time.sleep(0.05)
        idle = service.usage()
        assert isinstance(idle, ErrorEnvelope)
        assert idle.error.code == "no_enabled_providers"
        assert service.current_settings() is not None
        assert service.current_settings().enabled_providers == []  # type: ignore[union-attr]
    finally:
        gate.set()
        service.stop()


def test_reenable_does_not_apply_previous_generation(tmp_path: Path) -> None:
    factory = SpyFactory()
    gate = factory.block("codex")
    sequence = {"n": 0}

    def _result(*, now: int, deadline_seconds: float) -> ProviderCollectionSuccess:
        del now, deadline_seconds
        sequence["n"] += 1
        return _codex_ok(collected_at=sequence["n"])

    factory.set_result("codex", _result)
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    _save_settings(paths, "codex")
    service.start()
    try:
        _wait_until(
            lambda: factory.entered.get("codex") is not None and factory.entered["codex"].is_set()
        )
        service.apply_settings(SettingsWriteRequest(expected_revision=1, enabled_providers=[]))
        service.apply_settings(
            SettingsWriteRequest(expected_revision=2, enabled_providers=["codex"])
        )
        gate.set()
        _wait_collects(factory, 2)
        snapshot = _wait_usage_snapshot(service)
        assert snapshot.providers["codex"].collected_at == 2
    finally:
        gate.set()
        service.stop()


def test_settings_save_failure_does_not_commit_runtime(tmp_path: Path) -> None:
    factory = SpyFactory()
    factory.set_result("codex", _codex_ok())
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    _save_settings(paths, "codex")
    service.start()
    try:
        _wait_usage_snapshot(service)

        def _boom(_source: Path, _destination: Path) -> None:
            raise OSError(FAKE_SECRET)

        service._settings_store = SettingsStore(paths, replace=_boom)
        with pytest.raises(SettingsSaveError):
            service.apply_settings(
                SettingsWriteRequest(expected_revision=1, enabled_providers=["cursor"])
            )
        assert service.current_settings() is not None
        assert service.current_settings().enabled_providers == ["codex"]  # type: ignore[union-attr]
        assert "cursor" not in factory.collector_for_calls
        logs = _logs.getvalue()
        assert FAKE_SECRET not in logs
    finally:
        service.stop()


def test_cas_conflict_leaves_runtime_unchanged(tmp_path: Path) -> None:
    factory = SpyFactory()
    factory.set_result("codex", _codex_ok())
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    _save_settings(paths, "codex")
    service.start()
    try:
        _wait_usage_snapshot(service)
        with pytest.raises(Exception, match="revision"):
            service.apply_settings(
                SettingsWriteRequest(expected_revision=0, enabled_providers=["cursor"])
            )
        assert service.current_settings().enabled_providers == ["codex"]  # type: ignore[union-attr]
    finally:
        service.stop()


def test_restart_restores_settings_and_last_good(tmp_path: Path) -> None:
    factory = SpyFactory()
    factory.set_result("codex", _timeout_failure("codex", CODEX_SOURCE))
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    SnapshotCacheStore(paths).save(_fixture_snapshot())
    _save_settings(paths, "codex")
    service.start()
    try:
        _wait_collects(factory, 1)

        def _stale() -> bool:
            view = service.usage()
            return isinstance(view, UsageSnapshot) and view.providers["codex"].status == "stale"

        _wait_until(_stale)
        snapshot = service.usage()
        assert isinstance(snapshot, UsageSnapshot)
        assert snapshot.providers["codex"].collected_at == 1_999_999_980
        assert set(snapshot.providers) == {"codex"}
    finally:
        service.stop()

    factory2 = SpyFactory()
    factory2.set_result("codex", _codex_ok(collected_at=NOW + 10))
    clocks2 = FakeClocks(wall=NOW + 10)
    service2, _, _ = _make_service(tmp_path, factory2, clocks2)
    service2.start()
    try:
        recovered = _wait_usage_snapshot(service2)
        assert recovered.providers["codex"].status in {"ok", "stale"}

        def _ok() -> bool:
            view = service2.usage()
            return isinstance(view, UsageSnapshot) and view.providers["codex"].status == "ok"

        _wait_until(_ok)
        ready = service2.usage()
        assert isinstance(ready, UsageSnapshot)
        assert ready.providers["codex"].status == "ok"
        assert ready.providers["codex"].collected_at == NOW + 10
    finally:
        service2.stop()


def test_auth_missing_without_last_good_is_schema_valid_unavailable(tmp_path: Path) -> None:
    factory = SpyFactory()
    factory.set_result("cursor", _auth_missing("cursor", CURSOR_SOURCE))
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    _save_settings(paths, "cursor")
    service.start()
    try:
        snapshot = _wait_usage_snapshot(service)
        assert snapshot.providers["cursor"].status == "unavailable"
        payload = dump_usage_snapshot(snapshot)
        assert payload["providers"]["cursor"]["status"] == "unavailable"
    finally:
        service.stop()


def test_claude_enabled_waits_without_calling_factory(tmp_path: Path) -> None:
    factory = SpyFactory()
    factory.set_result("codex", _codex_ok())
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    _save_settings(paths, "claude")
    service.start()
    try:
        snapshot = _wait_usage_snapshot(service)
        assert snapshot.providers["claude"].status == "unavailable"
        assert snapshot.providers["claude"].error.category == "not_configured"
        assert "status-line" in snapshot.providers["claude"].error.message
        assert factory.collector_for_calls == []
        generation = service.provider_generation("claude")
        service.ingest_claude_event(_claude_ok(collected_at=NOW + 5), generation=generation - 1)
        still = service.usage()
        assert isinstance(still, UsageSnapshot)
        assert still.providers["claude"].status == "unavailable"
        service.ingest_claude_event(_claude_ok(collected_at=NOW + 5), generation=generation)
        updated = service.usage()
        assert isinstance(updated, UsageSnapshot)
        assert updated.providers["claude"].status == "ok"
        assert updated.providers["claude"].collected_at == NOW + 5
    finally:
        service.stop()


def test_disable_removes_provider_from_projection_without_refreshing_age(tmp_path: Path) -> None:
    factory = SpyFactory()
    factory.set_result("codex", _codex_ok())
    factory.set_result("cursor", _cursor_ok())
    clocks = FakeClocks()
    service, paths, _logs = _make_service(tmp_path, factory, clocks)
    SnapshotCacheStore(paths).save(_fixture_snapshot())
    _save_settings(paths, "codex", "cursor")
    service.start()
    try:
        _wait_usage_snapshot(service)
        original = service.usage()
        assert isinstance(original, UsageSnapshot)
        cursor_age = original.providers["cursor"].collected_at
        service.apply_settings(
            SettingsWriteRequest(expected_revision=1, enabled_providers=["codex"])
        )
        remaining = service.usage()
        assert isinstance(remaining, UsageSnapshot)
        assert set(remaining.providers) == {"codex"}
        factory.block("cursor")
        service.apply_settings(
            SettingsWriteRequest(expected_revision=2, enabled_providers=["codex", "cursor"])
        )
        restored = service.usage()
        assert isinstance(restored, UsageSnapshot)
        assert restored.providers["cursor"].collected_at == cursor_age
    finally:
        for event in factory._blocks.values():
            event.set()
        service.stop()


def test_second_instance_is_rejected_without_secrets(tmp_path: Path) -> None:
    factory = SpyFactory()
    clocks = FakeClocks()
    service, paths, logs = _make_service(tmp_path, factory, clocks)
    service.start()
    other = CollectorService(
        paths,
        wall_clock=clocks,
        monotonic_clock=clocks,
        wakeup=FakeClocks(),
        collector_factory=SpyFactory(),
        stderr=logs,
        shutdown_deadline_seconds=0.2,
    )
    try:
        with pytest.raises(ServiceAlreadyRunningError, match="already running") as excinfo:
            other.start()
        assert FAKE_SECRET not in str(excinfo.value)
        assert str(paths.config_dir) not in str(excinfo.value)
    finally:
        service.stop()


def test_shutdown_returns_within_deadline_while_job_blocked(tmp_path: Path) -> None:
    factory = SpyFactory()
    gate = factory.block("codex")
    factory.set_result("codex", _codex_ok())
    clocks = FakeClocks()
    service, paths, logs = _make_service(tmp_path, factory, clocks, shutdown_deadline_seconds=0.2)
    _save_settings(paths, "codex")
    service.start()
    _wait_until(
        lambda: factory.entered.get("codex") is not None and factory.entered["codex"].is_set()
    )
    started = time.monotonic()
    service.stop()
    elapsed = time.monotonic() - started
    assert elapsed < 1.5
    gate.set()
    time.sleep(0.05)
    assert FAKE_SECRET not in logs.getvalue()


def test_collector_exception_is_not_logged(tmp_path: Path) -> None:
    factory = SpyFactory()
    factory.set_result("codex", RuntimeError(FAKE_SECRET))
    clocks = FakeClocks()
    service, paths, logs = _make_service(tmp_path, factory, clocks)
    _save_settings(paths, "codex")
    service.start()
    try:
        snapshot = _wait_usage_snapshot(service)
        assert snapshot.providers["codex"].status == "error"
        text = logs.getvalue()
        assert FAKE_SECRET not in text
        assert "Traceback" not in text
        assert "result=failure" in text
        assert "category=internal" in text
    finally:
        service.stop()


def test_live_factory_does_not_collect_until_callable_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _boom(**_kwargs: object) -> None:
        calls.append("called")
        raise AssertionError("live collect")

    monkeypatch.setattr("agent_meter.providers.codex.collect", _boom)
    monkeypatch.setattr("agent_meter.providers.cursor.collect", _boom)
    factory = LazyLiveCollectorFactory()
    collector = factory.collector_for("codex")
    assert calls == []
    with pytest.raises(LookupError, match="event-driven"):
        factory.collector_for("claude")
    with pytest.raises(AssertionError, match="live collect"):
        collector(now=NOW, deadline_seconds=1)
    assert calls == ["called"]


def test_instance_lock_round_trip(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = ServiceInstanceLock(paths)
    first.acquire()
    second = ServiceInstanceLock(paths)
    with pytest.raises(ServiceAlreadyRunningError):
        second.acquire()
    first.release()
    second.acquire()
    second.release()
