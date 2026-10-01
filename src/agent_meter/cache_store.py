"""Versioned disk cache for normalized usage snapshots（PR #9）.

Responsibility: load/save a cache_version envelope, restore last-good into
memory, and remember persistence faults. Non-goals: merge/freshness policy,
HTTP, scheduler, or reading provider credentials.

Inputs: RuntimePaths, an injected clock, and a schema-valid UsageSnapshot.
Outputs: a stored snapshot, a public enabled projection, or a sanitized
cache fault. Units: UTC Unix seconds on the clock; file size in bytes.

Contract: `docs/contracts/cache.md`. Merge policy stays in `cache.py`.
Filesystem helpers are in `private_files.py`. GET /usage must receive the
projected UsageSnapshot, never this envelope.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError

from agent_meter.cache import project_enabled_snapshot
from agent_meter.freshness import Clock
from agent_meter.models import (
    ContractModel,
    ErrorSummary,
    UsageSnapshot,
    dump_usage_snapshot,
)
from agent_meter.paths import RuntimePaths
from agent_meter.private_files import (
    PrivateFileError,
    ReplaceFn,
    atomic_replace,
    is_absent,
    parse_json_object,
    prepare_private_dir,
    read_bounded_nofollow,
    reject_unsafe_dir,
    reject_unsafe_file,
)
from agent_meter.settings import Settings

PUBLIC_CACHE_SCHEMA_ID = "https://agent-meter.local/schemas/cache-v1.schema.json"
CACHE_VERSION: Literal[1] = 1
MAX_CACHE_BYTES = 256 * 1024

CACHE_READ_FAULT = ErrorSummary(
    category="cache",
    message="Usage cache could not be read",
    retryable=False,
    code="cache_unreadable",
)
CACHE_WRITE_FAULT = ErrorSummary(
    category="cache",
    message="Usage cache could not be saved",
    retryable=True,
    code="cache_unwritable",
)


class CacheSaveError(Exception):
    """Atomic cache replace failed. Memory last-good must stay in place."""

    code = "cache_error"

    def __init__(self) -> None:
        super().__init__("Usage cache could not be saved")


class DiskCacheEnvelope(ContractModel):
    """On-disk document. Only version metadata plus a usage snapshot."""

    # SECURITY: extra="forbid"；不得落地 raw_response、token 或 exception（PR #9）。
    cache_version: Literal[1]
    snapshot: UsageSnapshot


@dataclass(frozen=True)
class CacheLoadResult:
    """Disk read outcome. Missing file is a miss, not a persistence fault（PR #9）。"""

    snapshot: UsageSnapshot | None
    fault: ErrorSummary | None


class SnapshotCacheRepository(Protocol):
    """B2-03 injects this instead of importing filesystem I/O into merge policy（PR #9）。"""

    def load(self) -> CacheLoadResult:
        """Return the stored snapshot, a miss, or a sanitized fault."""
        ...

    def save(self, snapshot: UsageSnapshot) -> None:
        """Atomically persist a schema-valid snapshot. Raise CacheSaveError on I/O failure."""
        ...


def _os_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def dump_disk_cache_envelope(envelope: DiskCacheEnvelope) -> dict[str, object]:
    """JSON-ready envelope. GET /usage must never serialize this document（PR #9）。"""

    return {
        "cache_version": envelope.cache_version,
        "snapshot": dump_usage_snapshot(envelope.snapshot),
    }


class SnapshotCacheStore:
    """Persist DiskCacheEnvelope under RuntimePaths.snapshot_cache_file."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        replace: ReplaceFn | None = None,
    ) -> None:
        self._paths = paths
        self._replace = replace if replace is not None else _os_replace
        self._lock = threading.Lock()

    def load(self) -> CacheLoadResult:
        path = self._paths.snapshot_cache_file
        with self._lock:
            try:
                if is_absent(path):
                    if not is_absent(self._paths.snapshot_cache_dir):
                        reject_unsafe_dir(
                            self._paths.snapshot_cache_dir, require_private_mode=False
                        )
                    # FALLBACK: 缺檔是正常冷啟動，不是 cache_error（PR #9）。
                    return CacheLoadResult(snapshot=None, fault=None)
                if not is_absent(self._paths.snapshot_cache_dir):
                    reject_unsafe_dir(self._paths.snapshot_cache_dir, require_private_mode=True)
                reject_unsafe_file(path)
                raw = read_bounded_nofollow(path, MAX_CACHE_BYTES)
                payload = parse_json_object(raw)
                envelope = DiskCacheEnvelope.model_validate(payload)
                return CacheLoadResult(snapshot=envelope.snapshot, fault=None)
            except (PrivateFileError, OSError, ValidationError, ValueError):
                # FALLBACK: 損壞／未知版本／過大／symlink 忽略、留檔、不 crash（PR #9）。
                return CacheLoadResult(snapshot=None, fault=CACHE_READ_FAULT)

    def save(self, snapshot: UsageSnapshot) -> None:
        envelope = DiskCacheEnvelope(cache_version=CACHE_VERSION, snapshot=snapshot)
        payload = json.dumps(dump_disk_cache_envelope(envelope), indent=2, ensure_ascii=True) + "\n"
        with self._lock:
            try:
                prepare_private_dir(self._paths.snapshot_cache_dir)
                atomic_replace(
                    self._paths.snapshot_cache_file,
                    payload,
                    replace=self._replace,
                )
            except (PrivateFileError, OSError):
                # FALLBACK: replace 失敗不得清掉目的檔；呼叫端必須保留 memory last-good（PR #9）。
                raise CacheSaveError() from None


class PersistentSnapshotCache:
    """In-memory last-good plus disk store. Public output is an enabled projection."""

    def __init__(self, store: SnapshotCacheRepository, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        self.stored: UsageSnapshot | None = None
        self.persistence_fault: ErrorSummary | None = None

    def restore(self, settings: Settings) -> UsageSnapshot | None:
        """Load last-good from disk, then project to the current enabled set.

        Full last-good stays in `stored` even when some providers are disabled.
        Public snapshot must not refresh `collected_at` or resurrect disabled IDs（PR #9）。
        """

        result = self._store.load()
        if result.fault is not None:
            self.stored = None
            self.persistence_fault = result.fault
            return None
        self.persistence_fault = None
        self.stored = result.snapshot
        return self.public_snapshot(settings)

    def public_snapshot(self, settings: Settings) -> UsageSnapshot | None:
        if self.stored is None:
            return None
        return project_enabled_snapshot(
            self.stored,
            settings.enabled_providers,
            now=self._clock.now(),
        )

    def remember(self, snapshot: UsageSnapshot) -> None:
        """Keep memory last-good. Persist separately so a save failure cannot wipe it（PR #9）。"""

        self.stored = snapshot

    def persist(self) -> None:
        if self.stored is None:
            return
        try:
            self._store.save(self.stored)
        except CacheSaveError:
            # FALLBACK: 寫入失敗仍繼續用 memory last-good；health 才標 cache_error（PR #9）。
            self.persistence_fault = CACHE_WRITE_FAULT
            raise
        self.persistence_fault = None
