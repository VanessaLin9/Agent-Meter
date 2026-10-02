"""Resident Collector lifecycle, scheduling, and generation fencing（PR #10）.

Responsibility: start/stop one service instance, apply settings after CAS
persist, poll Codex/Cursor independently, and expose read-only usage/health
projections. Non-goals: FastAPI, Claude mailbox ingestion, new provider
auth, or a second merge/fallback policy.

Inputs: RuntimePaths, injected wall/monotonic clocks, a collector factory,
and SettingsWriteRequest. Outputs: UsageSnapshot or the existing error
envelope; HealthDocument; Settings. Units: poll interval/timeout/backoff
are seconds; generation is a per-provider integer; timestamps are UTC Unix
seconds.

Contract: `docs/contracts/collector-service.md` and
`docs/contracts/desktop-service.md`. Retry/cache/fallback stay in
`cache.py` / `cache_store.py`; this module only decides when to call them.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Thread
from typing import TextIO

from agent_meter.aggregation import build_snapshot
from agent_meter.cache import (
    ProviderCollectionFailure,
    ProviderCollectionSuccess,
    merge_collection_results,
)
from agent_meter.cache_store import CacheSaveError, PersistentSnapshotCache, SnapshotCacheStore
from agent_meter.clocks import (
    ConditionWakeup,
    MonotonicClock,
    SystemMonotonicClock,
    SystemWallClock,
    Wakeup,
    WallClock,
)
from agent_meter.freshness import StaleSettings
from agent_meter.instance_lock import ServiceInstanceLock
from agent_meter.models import UsageSnapshot
from agent_meter.orchestrator import ProviderCollector, invoke_collector
from agent_meter.paths import RuntimePaths
from agent_meter.provider_registry import LazyLiveCollectorFactory, ProviderCollectorFactory
from agent_meter.providers.claude import CLAUDE_PROVIDER_ID, CLAUDE_SOURCE
from agent_meter.providers.codex_rpc import CODEX_PROVIDER_ID, CODEX_SOURCE
from agent_meter.providers.cursor_rpc import CURSOR_PROVIDER_ID, CURSOR_SOURCE
from agent_meter.service_models import ErrorEnvelope, HealthDocument
from agent_meter.service_policy import (
    POLL_INTERVAL_SECONDS,
    POLL_TIMEOUT_SECONDS,
    POLLED_PROVIDER_IDS,
    SNAPSHOT_UNAVAILABLE,
    STALE_AFTER_SECONDS,
    health_document,
    poll_retry_delay_seconds,
    usage_error_envelope,
)
from agent_meter.settings import (
    ENABLED_PROVIDER_IDS,
    EnabledProvider,
    Settings,
    SettingsWriteRequest,
)
from agent_meter.settings_store import SettingsConfigError, SettingsStore

# CONTRACT: shutdown waits this long for in-flight jobs, then continues（PR #10）。
# Adapters must honor deadline_seconds; leftover threads cannot be killed.
SHUTDOWN_DEADLINE_SECONDS = POLL_TIMEOUT_SECONDS + 5.0

_PROVIDER_SOURCES: dict[str, str] = {
    CLAUDE_PROVIDER_ID: CLAUDE_SOURCE,
    CODEX_PROVIDER_ID: CODEX_SOURCE,
    CURSOR_PROVIDER_ID: CURSOR_SOURCE,
}

# FALLBACK: Claude 是 event source，不是 poller（PR #10）。B2-05 mailbox 到來前，
# 啟用但沒有 last-good 時用 not_configured／unavailable，不呼叫 Claude API。
CLAUDE_WAITING_FAILURE = ProviderCollectionFailure(
    provider_id=CLAUDE_PROVIDER_ID,
    source=CLAUDE_SOURCE,
    category="not_configured",
    message="Waiting for Claude status-line input",
    retryable=True,
    code="waiting_statusline",
)


class CollectorService:
    """Long-running enablement + poll core. HTTP handlers only call the getters."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        wall_clock: WallClock | None = None,
        monotonic_clock: MonotonicClock | None = None,
        wakeup: Wakeup | None = None,
        collector_factory: ProviderCollectorFactory | None = None,
        settings_store: SettingsStore | None = None,
        cache: PersistentSnapshotCache | None = None,
        instance_lock: ServiceInstanceLock | None = None,
        stderr: TextIO | None = None,
        shutdown_deadline_seconds: float = SHUTDOWN_DEADLINE_SECONDS,
        stale_settings: StaleSettings | None = None,
    ) -> None:
        self._paths = paths
        self._wall = wall_clock if wall_clock is not None else SystemWallClock()
        self._mono = monotonic_clock if monotonic_clock is not None else SystemMonotonicClock()
        self._wakeup = wakeup if wakeup is not None else ConditionWakeup()
        self._factory = (
            collector_factory
            if collector_factory is not None
            else LazyLiveCollectorFactory(stderr=stderr)
        )
        self._settings_store = (
            settings_store if settings_store is not None else SettingsStore(paths)
        )
        self._cache = (
            cache
            if cache is not None
            else PersistentSnapshotCache(SnapshotCacheStore(paths), self._wall)
        )
        self._instance_lock = (
            instance_lock if instance_lock is not None else ServiceInstanceLock(paths)
        )
        self._stderr = stderr
        self._shutdown_deadline_seconds = shutdown_deadline_seconds
        self._stale_settings = (
            stale_settings
            if stale_settings is not None
            else StaleSettings(default_stale_after_seconds=STALE_AFTER_SECONDS)
        )
        self._state_lock = Lock()
        self._apply_lock = Lock()
        self._running = False
        self._started = False
        self._runtime_settings: Settings | None = None
        self._enabled: tuple[EnabledProvider, ...] = ()
        self._generations: dict[EnabledProvider, int] = dict.fromkeys(ENABLED_PROVIDER_IDS, 0)
        self._in_flight: dict[EnabledProvider, int] = {}
        self._next_due: dict[EnabledProvider, float] = {}
        self._last_start: dict[EnabledProvider, float] = {}
        self._failures: dict[EnabledProvider, int] = {}
        self._collectors: dict[EnabledProvider, ProviderCollector] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._scheduler: Thread | None = None

    def start(self) -> None:
        """Load settings + cache, take the instance lock, and start the scheduler.

        SECURITY: enabled_providers=[] 零 live adapter I/O（PR #10）。session 讀取
        延後到 enabled poll job 真正執行。
        """

        with self._apply_lock:
            if self._running:
                return
            self._instance_lock.acquire()
            try:
                self._restore()
                self._pool = ThreadPoolExecutor(
                    max_workers=len(POLLED_PROVIDER_IDS),
                    thread_name_prefix="agent-meter-collect",
                )
                self._running = True
                self._started = True
                self._scheduler = Thread(
                    target=self._run_scheduler,
                    name="agent-meter-scheduler",
                    daemon=True,
                )
                self._scheduler.start()
            except Exception:
                self._running = False
                self._started = False
                if self._pool is not None:
                    self._pool.shutdown(wait=False, cancel_futures=True)
                    self._pool = None
                self._instance_lock.release()
                raise
        self._wakeup.notify()

    def stop(self) -> None:
        """Stop scheduling, isolate in-flight generations, flush cache, release lock."""

        with self._apply_lock:
            if not self._started:
                return
            with self._state_lock:
                self._running = False
                for provider_id in ENABLED_PROVIDER_IDS:
                    self._bump_generation(provider_id)
                # CONTRACT: stop 後必須清空 enabled，否則同一 instance 再 start
                # 時 _commit_enabled 會以為沒有新增 provider，就不排程（PR #10）。
                self._enabled = ()
                self._runtime_settings = None
                self._next_due.clear()
                self._failures.clear()
                self._collectors.clear()
                self._last_start.clear()
            self._wakeup.notify()
            scheduler = self._scheduler
            pool = self._pool
            self._scheduler = None
            self._pool = None
            if scheduler is not None:
                scheduler.join(timeout=self._shutdown_deadline_seconds)
            self._wait_for_idle()
            if pool is not None:
                with self._state_lock:
                    idle = not self._in_flight
                pool.shutdown(wait=idle, cancel_futures=True)
            try:
                if self._cache.stored is not None:
                    self._cache.persist()
            except CacheSaveError:
                pass
            self._instance_lock.release()
            self._started = False

    def apply_settings(self, request: SettingsWriteRequest) -> Settings:
        """Persist first, then commit runtime. Failure leaves the previous generation."""

        # CONTRACT: persist 成功後 runtime 才生效；成功回傳＝新清單已在跑（PR #10）。
        with self._apply_lock:
            saved = self._settings_store.save(request)
            with self._state_lock:
                self._commit_enabled(saved)
            self._wakeup.notify()
            return saved

    def usage(self) -> UsageSnapshot | ErrorEnvelope:
        """Read the current projection and re-evaluate age. Never starts a collect.

        CONTRACT: GET consumer 不得觸發 provider request（PR #10）。
        """

        try:
            with self._state_lock:
                settings = self._runtime_settings
                snapshot = None if settings is None else self._cache.public_snapshot(settings)
            envelope = usage_error_envelope(settings=settings, snapshot=snapshot)
            if envelope is not None:
                return envelope
            assert snapshot is not None
            return snapshot
        except Exception:
            # SECURITY: do not stringify the exception; it may contain locals.
            return ErrorEnvelope(error=SNAPSHOT_UNAVAILABLE)

    def health(self) -> HealthDocument:
        with self._state_lock:
            return health_document(
                settings=self._runtime_settings,
                cache_fault=self._cache.persistence_fault is not None,
            )

    def current_settings(self) -> Settings | None:
        with self._state_lock:
            return self._runtime_settings

    def provider_generation(self, provider_id: EnabledProvider) -> int:
        with self._state_lock:
            return self._generations[provider_id]

    def ingest_claude_event(
        self,
        result: ProviderCollectionSuccess | ProviderCollectionFailure,
        *,
        generation: int,
    ) -> None:
        """Apply a typed Claude event. B2-05 owns mailbox I/O; this only fences（PR #10）。"""

        sanitized = invoke_collector(
            CLAUDE_PROVIDER_ID,
            lambda *, now, deadline_seconds: result,
            now=self._wall.now(),
            deadline_seconds=POLL_TIMEOUT_SECONDS,
        )
        with self._state_lock:
            if (
                not self._running
                or CLAUDE_PROVIDER_ID not in self._enabled
                or self._generations[CLAUDE_PROVIDER_ID] != generation
            ):
                return
            self._apply_merge(sanitized)

    def _restore(self) -> None:
        try:
            settings = self._settings_store.load()
        except SettingsConfigError:
            with self._state_lock:
                self._runtime_settings = None
                self._enabled = ()
            return
        self._cache.restore(settings)
        with self._state_lock:
            self._commit_enabled(settings)

    def _commit_enabled(self, settings: Settings) -> None:
        previous = set(self._enabled)
        new = set(settings.enabled_providers)
        removed = previous - new
        added = new - previous
        self._runtime_settings = settings
        for provider_id in removed:
            # CONTRACT: disable 與 re-enable 都 bump generation，晚到結果不可回流（PR #10）。
            self._bump_generation(provider_id)
            self._next_due.pop(provider_id, None)
            self._failures.pop(provider_id, None)
            self._collectors.pop(provider_id, None)
        self._enabled = tuple(settings.enabled_providers)
        for provider_id in added:
            self._bump_generation(provider_id)
            self._failures[provider_id] = 0
            if provider_id == CLAUDE_PROVIDER_ID:
                self._seed_claude_waiting()
            elif provider_id in POLLED_PROVIDER_IDS:
                self._next_due[provider_id] = self._mono.monotonic()

    def _seed_claude_waiting(self) -> None:
        stored = self._cache.stored
        if stored is not None and CLAUDE_PROVIDER_ID in stored.providers:
            return
        self._apply_merge(CLAUDE_WAITING_FAILURE)

    def _bump_generation(self, provider_id: EnabledProvider) -> None:
        self._generations[provider_id] = self._generations[provider_id] + 1

    def _run_scheduler(self) -> None:
        while True:
            with self._state_lock:
                if not self._running:
                    return
                due = self._due_providers()
                timeout = self._sleep_timeout()
            for provider_id in due:
                self._dispatch(provider_id)
            self._wakeup.wait(timeout)

    def _due_providers(self) -> tuple[EnabledProvider, ...]:
        now = self._mono.monotonic()
        due: list[EnabledProvider] = []
        for provider_id in self._enabled:
            if provider_id not in POLLED_PROVIDER_IDS:
                continue
            if self._in_flight.get(provider_id) == self._generations[provider_id]:
                continue
            scheduled = self._next_due.get(provider_id)
            if scheduled is not None and scheduled <= now:
                due.append(provider_id)
        return tuple(due)

    def _sleep_timeout(self) -> float | None:
        now = self._mono.monotonic()
        waits: list[float] = []
        for provider_id in self._enabled:
            if (
                provider_id not in POLLED_PROVIDER_IDS
                or self._in_flight.get(provider_id) == self._generations[provider_id]
            ):
                continue
            scheduled = self._next_due.get(provider_id)
            if scheduled is None:
                continue
            waits.append(max(0.0, scheduled - now))
        if not waits:
            return None
        return min(waits)

    def _dispatch(self, provider_id: EnabledProvider) -> None:
        with self._state_lock:
            if (
                not self._running
                or provider_id not in self._enabled
                or self._in_flight.get(provider_id) == self._generations[provider_id]
                or provider_id not in POLLED_PROVIDER_IDS
            ):
                return
            generation = self._generations[provider_id]
            self._in_flight[provider_id] = generation
            self._last_start[provider_id] = self._mono.monotonic()
        pool = self._pool
        if pool is None:
            self._finish_job(provider_id, generation, self._internal_failure(provider_id))
            return
        try:
            collector = self._collector_for(provider_id)
            pool.submit(self._run_job, provider_id, generation, collector)
        except Exception:
            self._finish_job(provider_id, generation, self._internal_failure(provider_id))

    def _collector_for(self, provider_id: EnabledProvider) -> ProviderCollector:
        with self._state_lock:
            cached = self._collectors.get(provider_id)
        if cached is not None:
            return cached
        collector = self._factory.collector_for(provider_id)
        with self._state_lock:
            self._collectors[provider_id] = collector
        return collector

    def _run_job(
        self,
        provider_id: EnabledProvider,
        generation: int,
        collector: ProviderCollector,
    ) -> None:
        started = self._mono.monotonic()
        result = invoke_collector(
            provider_id,
            collector,
            now=self._wall.now(),
            deadline_seconds=POLL_TIMEOUT_SECONDS,
        )
        duration = self._mono.monotonic() - started
        self._finish_job(provider_id, generation, result, duration)

    def _finish_job(
        self,
        provider_id: EnabledProvider,
        generation: int,
        result: ProviderCollectionSuccess | ProviderCollectionFailure,
        duration: float = 0.0,
    ) -> None:
        discarded = False
        retry = 0
        with self._state_lock:
            if self._in_flight.get(provider_id) == generation:
                self._in_flight.pop(provider_id, None)
            if (
                not self._running
                or provider_id not in self._enabled
                or self._generations.get(provider_id) != generation
            ):
                # CONTRACT: 舊 generation 的 success／failure 都不寫 cache（PR #10）。
                discarded = True
            else:
                self._apply_merge(result)
                if isinstance(result, ProviderCollectionSuccess):
                    self._failures[provider_id] = 0
                    delay = float(POLL_INTERVAL_SECONDS)
                    start = self._last_start.get(provider_id, self._mono.monotonic())
                    nxt = start + delay
                    now = self._mono.monotonic()
                    self._next_due[provider_id] = now if nxt < now else nxt
                else:
                    self._failures[provider_id] = self._failures.get(provider_id, 0) + 1
                    retry = self._failures[provider_id]
                    delay = float(poll_retry_delay_seconds(retry))
                    self._next_due[provider_id] = self._mono.monotonic() + delay
        self._log_collection(
            provider_id, result, duration=duration, retry=retry, discarded=discarded
        )
        self._wakeup.notify()

    def _apply_merge(self, result: ProviderCollectionSuccess | ProviderCollectionFailure) -> None:
        previous = dict(self._cache.stored.providers) if self._cache.stored is not None else {}
        merged = merge_collection_results(previous, (result,), settings=self._stale_settings)
        if not merged:
            return
        snapshot = build_snapshot(merged, now=self._wall.now())
        self._cache.remember(snapshot)
        try:
            self._cache.persist()
        except CacheSaveError:
            pass

    def _internal_failure(self, provider_id: str) -> ProviderCollectionFailure:
        return ProviderCollectionFailure(
            provider_id=provider_id,
            source=_PROVIDER_SOURCES.get(provider_id, "collector"),
            category="internal",
            message="Provider collection failed",
            retryable=True,
        )

    def _wait_for_idle(self) -> None:
        deadline = time.monotonic() + self._shutdown_deadline_seconds
        while time.monotonic() < deadline:
            with self._state_lock:
                if not self._in_flight:
                    return
            time.sleep(0.01)

    def _log_collection(
        self,
        provider_id: str,
        result: ProviderCollectionSuccess | ProviderCollectionFailure,
        *,
        duration: float,
        retry: int,
        discarded: bool,
    ) -> None:
        if self._stderr is None:
            return
        if discarded:
            status = "discarded"
            category = "ok" if isinstance(result, ProviderCollectionSuccess) else result.category
        elif isinstance(result, ProviderCollectionSuccess):
            status = "ok"
            category = "ok"
        else:
            status = "failure"
            category = result.category
        duration_ms = int(round(duration * 1000))
        self._stderr.write(
            f"{provider_id} result={status} duration_ms={duration_ms} category={category} retry={retry}\n"
        )
