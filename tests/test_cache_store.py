"""Persistent snapshot cache tests. Clock, network, and live providers are unused."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from jsonschema.validators import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError

from agent_meter.cache_store import (
    CACHE_READ_FAULT,
    CACHE_WRITE_FAULT,
    MAX_CACHE_BYTES,
    PUBLIC_CACHE_SCHEMA_ID,
    CacheSaveError,
    DiskCacheEnvelope,
    PersistentSnapshotCache,
    SnapshotCacheStore,
    dump_disk_cache_envelope,
)
from agent_meter.freshness import FrozenClock
from agent_meter.models import UsageSnapshot, dump_usage_snapshot, parse_usage_snapshot
from agent_meter.paths import RuntimePaths, resolve_runtime_paths
from agent_meter.service_policy import health_document, usage_error
from agent_meter.settings import EnabledProvider, Settings, first_install_settings, parse_settings

FAKE_SECRET = "PLANTED_PROVIDER_TOKEN_VALUE_DO_NOT_EMIT"
NOW = 2_000_000_000
FIXTURE_SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "contracts" / "ok.json"
CACHE_SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "cache-v1.schema.json"


def _paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_dir=tmp_path / "config", cache_dir=tmp_path / "cache")


def _snapshot() -> UsageSnapshot:
    return parse_usage_snapshot(json.loads(FIXTURE_SNAPSHOT.read_text(encoding="utf-8")))


def _enabled(*providers: EnabledProvider, revision: int = 1) -> Settings:
    return parse_settings(
        {
            "settings_version": 1,
            "revision": revision,
            "enabled_providers": list(providers),
        }
    )


def _prepare_existing_cache_dirs(paths: RuntimePaths) -> None:
    paths.cache_dir.mkdir(mode=0o700)
    os.chmod(paths.cache_dir, 0o700)
    paths.snapshot_cache_dir.mkdir(mode=0o700)
    os.chmod(paths.snapshot_cache_dir, 0o700)


def test_missing_cache_is_a_normal_start(tmp_path: Path) -> None:
    store = SnapshotCacheStore(_paths(tmp_path))
    result = store.load()
    assert result.snapshot is None
    assert result.fault is None
    assert not (_paths(tmp_path).snapshot_cache_dir).exists()
    assert not (_paths(tmp_path).cache_dir).exists()


def test_save_then_new_store_load_round_trips(tmp_path: Path) -> None:
    snapshot = _snapshot()
    SnapshotCacheStore(_paths(tmp_path)).save(snapshot)
    loaded = SnapshotCacheStore(_paths(tmp_path)).load()
    assert loaded.fault is None
    assert loaded.snapshot is not None
    assert dump_usage_snapshot(loaded.snapshot) == dump_usage_snapshot(snapshot)
    cache_file = _paths(tmp_path).snapshot_cache_file
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(_paths(tmp_path).cache_dir.stat().st_mode) == 0o700
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert payload == dump_disk_cache_envelope(
        DiskCacheEnvelope(cache_version=1, snapshot=snapshot)
    )


def test_first_save_succeeds_when_library_caches_is_missing(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(home=tmp_path)
    caches = tmp_path / "Library" / "Caches"
    assert not caches.exists()
    SnapshotCacheStore(paths).save(_snapshot())
    assert stat.S_IMODE(paths.cache_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.snapshot_cache_dir.stat().st_mode) == 0o700
    assert caches.is_dir()
    assert not caches.is_symlink()
    loaded = SnapshotCacheStore(paths).load()
    assert loaded.fault is None
    assert loaded.snapshot is not None


def test_restore_recomputes_freshness_without_touching_collected_at(tmp_path: Path) -> None:
    snapshot = _snapshot()
    original = snapshot.providers["codex"].collected_at
    assert original is not None
    SnapshotCacheStore(_paths(tmp_path)).save(snapshot)
    later = FrozenClock(unix_seconds=original + snapshot.providers["codex"].stale_after_seconds)
    cache = PersistentSnapshotCache(SnapshotCacheStore(_paths(tmp_path)), later)
    public = cache.restore(_enabled("codex", "cursor", "claude"))
    assert public is not None
    assert public.generated_at == later.unix_seconds
    assert public.providers["codex"].collected_at == original
    assert public.providers["codex"].status == "stale"
    assert cache.stored is not None
    assert cache.stored.providers["codex"].status == "ok"


def test_clock_rollback_keeps_ok_fresh(tmp_path: Path) -> None:
    snapshot = _snapshot()
    SnapshotCacheStore(_paths(tmp_path)).save(snapshot)
    collected_at = snapshot.providers["codex"].collected_at
    assert collected_at is not None
    earlier = FrozenClock(unix_seconds=collected_at - 10)
    public = PersistentSnapshotCache(SnapshotCacheStore(_paths(tmp_path)), earlier).restore(
        _enabled("codex")
    )
    assert public is not None
    assert public.providers["codex"].status == "ok"
    assert public.providers["codex"].collected_at == snapshot.providers["codex"].collected_at


def test_disabled_provider_does_not_appear_after_restart(tmp_path: Path) -> None:
    SnapshotCacheStore(_paths(tmp_path)).save(_snapshot())
    cache = PersistentSnapshotCache(SnapshotCacheStore(_paths(tmp_path)), FrozenClock(NOW))
    public = cache.restore(_enabled("codex"))
    assert public is not None
    assert set(public.providers) == {"codex"}
    assert set(cache.stored.providers) == {"codex", "cursor", "claude"}  # type: ignore[union-attr]
    assert usage_error(settings=_enabled("codex"), snapshot=public) is None


def test_all_disabled_returns_no_snapshot_not_schema_error(tmp_path: Path) -> None:
    SnapshotCacheStore(_paths(tmp_path)).save(_snapshot())
    cache = PersistentSnapshotCache(SnapshotCacheStore(_paths(tmp_path)), FrozenClock(NOW))
    public = cache.restore(first_install_settings())
    assert public is None
    error = usage_error(settings=first_install_settings(), snapshot=public)
    assert error is not None
    assert error.code == "no_enabled_providers"


def test_malformed_cache_with_secret_is_ignored_not_deleted(tmp_path: Path) -> None:
    store = SnapshotCacheStore(_paths(tmp_path))
    paths = _paths(tmp_path)
    _prepare_existing_cache_dirs(paths)
    cache_file = paths.snapshot_cache_file
    cache_file.write_text(f'{{"token": "{FAKE_SECRET}"', encoding="utf-8")
    os.chmod(cache_file, 0o600)
    result = store.load()
    assert result.snapshot is None
    assert result.fault == CACHE_READ_FAULT
    assert FAKE_SECRET not in str(result.fault)
    assert FAKE_SECRET in cache_file.read_text(encoding="utf-8")
    cache = PersistentSnapshotCache(store, FrozenClock(NOW))
    assert cache.restore(_enabled("codex")) is None
    assert cache.persistence_fault == CACHE_READ_FAULT
    health = health_document(settings=_enabled("codex"), cache_fault=True)
    assert health.state == "degraded"
    assert health.error is not None
    assert health.error.code == "cache_error"
    assert FAKE_SECRET not in health.error.message


def test_unknown_version_and_oversized_and_symlink_are_faults(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _prepare_existing_cache_dirs(paths)
    cache_file = paths.snapshot_cache_file
    cache_file.write_text(
        json.dumps({"cache_version": 2, "snapshot": dump_usage_snapshot(_snapshot())}),
        encoding="utf-8",
    )
    os.chmod(cache_file, 0o600)
    assert SnapshotCacheStore(paths).load().fault == CACHE_READ_FAULT
    assert cache_file.exists()

    cache_file.write_bytes((FAKE_SECRET + "x" * MAX_CACHE_BYTES).encode("utf-8"))
    os.chmod(cache_file, 0o600)
    oversized = SnapshotCacheStore(paths).load()
    assert oversized.fault == CACHE_READ_FAULT
    assert FAKE_SECRET not in str(oversized.fault)

    os.chmod(cache_file, 0o644)
    assert SnapshotCacheStore(paths).load().fault == CACHE_READ_FAULT
    assert cache_file.exists()

    cache_file.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"token": FAKE_SECRET}), encoding="utf-8")
    os.symlink(outside, cache_file)
    linked = SnapshotCacheStore(paths).load()
    assert linked.fault == CACHE_READ_FAULT
    assert FAKE_SECRET not in str(linked.fault)
    assert FAKE_SECRET in outside.read_text(encoding="utf-8")


def test_loose_cache_root_mode_is_a_fault_until_save_repairs_it(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    snapshot = _snapshot()
    SnapshotCacheStore(paths).save(snapshot)
    os.chmod(paths.cache_dir, 0o755)
    assert SnapshotCacheStore(paths).load().fault == CACHE_READ_FAULT
    SnapshotCacheStore(paths).save(snapshot)
    assert stat.S_IMODE(paths.cache_dir.stat().st_mode) == 0o700
    repaired = SnapshotCacheStore(paths).load()
    assert repaired.fault is None
    assert repaired.snapshot is not None


def test_symlink_cache_root_is_not_followed_on_load_or_save(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    os.chmod(outside, 0o700)
    nested = outside / "snapshots"
    nested.mkdir(mode=0o700)
    os.chmod(nested, 0o700)
    planted = nested / "snapshot.json"
    planted.write_text(json.dumps({"token": FAKE_SECRET}), encoding="utf-8")
    os.chmod(planted, 0o600)
    os.symlink(outside, paths.cache_dir)
    loaded = SnapshotCacheStore(paths).load()
    assert loaded.snapshot is None
    assert loaded.fault == CACHE_READ_FAULT
    assert FAKE_SECRET not in str(loaded.fault)
    with pytest.raises(CacheSaveError):
        SnapshotCacheStore(paths).save(_snapshot())
    assert FAKE_SECRET in planted.read_text(encoding="utf-8")
    assert json.loads(planted.read_text(encoding="utf-8")) == {"token": FAKE_SECRET}


def test_replace_abort_keeps_previous_and_cleans_temp(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    previous = _snapshot()
    SnapshotCacheStore(paths).save(previous)

    def _boom(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replace failure")

    failing = SnapshotCacheStore(paths, replace=_boom)
    with pytest.raises(CacheSaveError):
        failing.save(previous)
    loaded = SnapshotCacheStore(paths).load()
    assert loaded.snapshot is not None
    assert dump_usage_snapshot(loaded.snapshot) == dump_usage_snapshot(previous)
    assert list(paths.snapshot_cache_dir.glob(".snapshot.json.*.tmp")) == []


def test_save_failure_keeps_memory_last_good_and_sets_fault(tmp_path: Path) -> None:
    snapshot = _snapshot()
    paths = _paths(tmp_path)

    def _boom(_source: Path, _destination: Path) -> None:
        raise OSError("disk full")

    failing = PersistentSnapshotCache(SnapshotCacheStore(paths, replace=_boom), FrozenClock(NOW))
    failing.remember(snapshot)
    with pytest.raises(CacheSaveError):
        failing.persist()
    assert failing.stored is snapshot
    assert failing.persistence_fault == CACHE_WRITE_FAULT

    recovered = PersistentSnapshotCache(SnapshotCacheStore(paths), FrozenClock(NOW))
    recovered.remember(snapshot)
    recovered.persist()
    assert recovered.persistence_fault is None
    loaded = SnapshotCacheStore(paths).load()
    assert loaded.snapshot is not None


def test_concurrent_load_during_save_never_returns_partial_json(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = _snapshot()
    SnapshotCacheStore(paths).save(first)
    in_replace = threading.Event()
    release = threading.Event()

    def gated_replace(source: Path, destination: Path) -> None:
        in_replace.set()
        assert release.wait(timeout=1)
        os.replace(source, destination)

    writer = SnapshotCacheStore(paths, replace=gated_replace)
    reader = SnapshotCacheStore(paths)

    def saver() -> None:
        writer.save(first)

    thread = threading.Thread(target=saver)
    thread.start()
    assert in_replace.wait(timeout=1)
    loaded = reader.load()
    assert loaded.fault is None
    assert loaded.snapshot is not None
    parse_usage_snapshot(dump_usage_snapshot(loaded.snapshot))
    release.set()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_cache_envelope_schema_rejects_usage_payload_and_secrets() -> None:
    snapshot = _snapshot()
    envelope = dump_disk_cache_envelope(DiskCacheEnvelope(cache_version=1, snapshot=snapshot))
    schema = json.loads(CACHE_SCHEMA.read_text(encoding="utf-8"))
    assert isinstance(schema, dict)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(envelope)
    parsed = DiskCacheEnvelope.model_validate(envelope)
    assert dump_disk_cache_envelope(parsed) == envelope
    assert PUBLIC_CACHE_SCHEMA_ID.endswith("cache-v1.schema.json")
    with pytest.raises(PydanticValidationError):
        parse_usage_snapshot(envelope)
    with pytest.raises(PydanticValidationError):
        DiskCacheEnvelope.model_validate(dump_usage_snapshot(snapshot))
    smuggled = dict(envelope)
    smuggled["access_token"] = FAKE_SECRET
    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema).validate(smuggled)


def test_extra_secret_field_is_a_fault_and_file_is_kept(tmp_path: Path) -> None:
    envelope = dump_disk_cache_envelope(DiskCacheEnvelope(cache_version=1, snapshot=_snapshot()))
    envelope["access_token"] = FAKE_SECRET
    paths = _paths(tmp_path)
    _prepare_existing_cache_dirs(paths)
    cache_file = paths.snapshot_cache_file
    cache_file.write_text(json.dumps(envelope), encoding="utf-8")
    os.chmod(cache_file, 0o600)
    result = SnapshotCacheStore(paths).load()
    assert result.snapshot is None
    assert result.fault == CACHE_READ_FAULT
    assert FAKE_SECRET not in str(result.fault)
    assert FAKE_SECRET in cache_file.read_text(encoding="utf-8")
