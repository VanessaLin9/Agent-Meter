"""Settings store filesystem tests. Clock and network are unused."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_meter.paths import resolve_runtime_paths
from agent_meter.settings import (
    SettingsWriteRequest,
    dump_settings,
    first_install_settings,
    parse_settings,
    parse_settings_write,
)
from agent_meter.settings_store import (
    MAX_SETTINGS_BYTES,
    SettingsConfigError,
    SettingsRevisionConflict,
    SettingsSaveError,
    SettingsStore,
)

FAKE_SECRET = "PLANTED_PROVIDER_TOKEN_VALUE_DO_NOT_EMIT"


def _store(tmp_path: Path, *, replace: Callable[[Path, Path], None] | None = None) -> SettingsStore:
    paths = resolve_runtime_paths(
        config_dir=tmp_path / "config",
        cache_dir=tmp_path / "cache",
    )
    if replace is None:
        return SettingsStore(paths)
    return SettingsStore(paths, replace=replace)


def _write_request(*providers: str, expected_revision: int) -> SettingsWriteRequest:
    return parse_settings_write(
        {"expected_revision": expected_revision, "enabled_providers": list(providers)}
    )


def test_missing_file_is_first_install_without_creating_runtime(tmp_path: Path) -> None:
    store = _store(tmp_path)
    loaded = store.load()
    assert loaded == first_install_settings()
    assert not (tmp_path / "config").exists()
    assert not (tmp_path / "cache").exists()


def test_round_trip_survives_new_store_instance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.save(_write_request("codex", expected_revision=0))
    assert saved.revision == 1
    assert saved.enabled_providers == ["codex"]
    reloaded = SettingsStore(
        resolve_runtime_paths(config_dir=tmp_path / "config", cache_dir=tmp_path / "cache")
    ).load()
    assert reloaded == saved
    payload = json.loads((tmp_path / "config" / "settings.json").read_text(encoding="utf-8"))
    assert payload == dump_settings(saved)
    assert parse_settings(payload) == saved


def test_cas_conflict_does_not_change_disk(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.save(_write_request("codex", expected_revision=0))
    before = (tmp_path / "config" / "settings.json").read_bytes()
    with pytest.raises(SettingsRevisionConflict) as exc_info:
        store.save(_write_request("cursor", expected_revision=0))
    assert exc_info.value.code == "revision_conflict"
    assert (tmp_path / "config" / "settings.json").read_bytes() == before
    assert store.load() == first


def test_atomic_replace_failure_keeps_previous_document(tmp_path: Path) -> None:
    store = _store(tmp_path)
    previous = store.save(_write_request("codex", expected_revision=0))
    before = (tmp_path / "config" / "settings.json").read_bytes()

    def _boom(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replace failure")

    failing = _store(tmp_path, replace=_boom)
    with pytest.raises(SettingsSaveError) as exc_info:
        failing.save(_write_request("cursor", expected_revision=1))
    assert exc_info.value.code == "save_failed"
    assert (tmp_path / "config" / "settings.json").read_bytes() == before
    assert store.load() == previous
    leftovers = list((tmp_path / "config").glob(".settings.json.*.tmp"))
    assert leftovers == []


def test_private_modes_are_applied_on_save(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_write_request("claude", expected_revision=0))
    config_dir = tmp_path / "config"
    settings_file = config_dir / "settings.json"
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings_file.stat().st_mode) == 0o600


def test_world_readable_file_is_config_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_write_request("codex", expected_revision=0))
    settings_file = tmp_path / "config" / "settings.json"
    os.chmod(settings_file, 0o644)
    before = settings_file.read_text(encoding="utf-8")
    with pytest.raises(SettingsConfigError) as exc_info:
        store.load()
    assert exc_info.value.code == "config_error"
    assert settings_file.read_text(encoding="utf-8") == before


def test_symlink_settings_file_is_rejected_without_following(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    target = tmp_path / "outside.json"
    target.write_text(
        json.dumps(
            {
                "token": FAKE_SECRET,
                "settings_version": 1,
                "revision": 9,
                "enabled_providers": [],
            }
        ),
        encoding="utf-8",
    )
    os.symlink(target, config_dir / "settings.json")
    with pytest.raises(SettingsConfigError) as exc_info:
        store.load()
    rendered = str(exc_info.value)
    assert FAKE_SECRET not in rendered
    assert target.read_text(encoding="utf-8")
    with pytest.raises(SettingsConfigError):
        store.save(_write_request("codex", expected_revision=0))
    assert FAKE_SECRET in target.read_text(encoding="utf-8")


def test_symlink_config_dir_is_not_first_install(tmp_path: Path) -> None:
    real_dir = tmp_path / "real-config"
    real_dir.mkdir()
    os.symlink(real_dir, tmp_path / "config")
    store = _store(tmp_path)
    with pytest.raises(SettingsConfigError):
        store.load()
    with pytest.raises(SettingsConfigError):
        store.save(_write_request("codex", expected_revision=0))
    assert list(real_dir.iterdir()) == []


def test_unknown_version_file_is_preserved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    settings_file = config_dir / "settings.json"
    damaged = '{"settings_version": 2, "revision": 0, "enabled_providers": []}\n'
    settings_file.write_text(damaged, encoding="utf-8")
    os.chmod(settings_file, 0o600)
    with pytest.raises(SettingsConfigError):
        store.load()
    assert settings_file.read_text(encoding="utf-8") == damaged
    with pytest.raises(SettingsConfigError):
        store.save(_write_request("codex", expected_revision=0))
    assert settings_file.read_text(encoding="utf-8") == damaged


def test_malformed_file_with_planted_secret_is_not_echoed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    settings_file = config_dir / "settings.json"
    settings_file.write_text(f'{{"token": "{FAKE_SECRET}"', encoding="utf-8")
    os.chmod(settings_file, 0o600)
    with pytest.raises(SettingsConfigError) as exc_info:
        store.load()
    assert FAKE_SECRET not in str(exc_info.value)
    assert FAKE_SECRET not in repr(exc_info.value)
    assert FAKE_SECRET in settings_file.read_text(encoding="utf-8")


def test_oversized_file_is_config_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    settings_file = config_dir / "settings.json"
    settings_file.write_bytes((FAKE_SECRET + "x" * MAX_SETTINGS_BYTES).encode("utf-8"))
    os.chmod(settings_file, 0o600)
    with pytest.raises(SettingsConfigError) as exc_info:
        store.load()
    assert FAKE_SECRET not in str(exc_info.value)
    assert settings_file.stat().st_size > MAX_SETTINGS_BYTES


def test_disable_all_providers_is_empty_list(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(_write_request("codex", "cursor", expected_revision=0))
    saved = store.save(_write_request(expected_revision=1))
    assert saved.enabled_providers == []
    assert saved.revision == 2
    assert store.load() == saved
