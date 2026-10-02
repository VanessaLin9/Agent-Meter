"""Runtime path resolver tests. Never touch the real user Library."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_meter.paths import APP_DIR_NAME, resolve_runtime_paths


def test_macos_library_paths_are_composed_from_injected_home(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(home=tmp_path)
    assert paths.config_dir == tmp_path / "Library" / "Application Support" / APP_DIR_NAME
    assert paths.cache_dir == tmp_path / "Library" / "Caches" / APP_DIR_NAME
    assert paths.settings_file == paths.config_dir / "settings.json"
    assert paths.snapshot_cache_dir == paths.cache_dir / "snapshots"
    assert paths.snapshot_cache_file == paths.snapshot_cache_dir / "snapshot.json"
    assert paths.mailbox_dir == paths.cache_dir / "mailbox"
    assert paths.service_lock_file == paths.config_dir / "service.lock"
    assert paths.config_dir != Path.cwd()
    assert Path.cwd() not in paths.config_dir.parents


def test_explicit_directories_do_not_call_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(*_args: object) -> Path:
        raise AssertionError("Path.home must not be used when dirs are injected")

    monkeypatch.setattr(Path, "home", _fail)
    config_dir = tmp_path / "config"
    cache_dir = tmp_path / "cache"
    paths = resolve_runtime_paths(config_dir=config_dir, cache_dir=cache_dir)
    assert paths.config_dir == config_dir
    assert paths.cache_dir == cache_dir


def test_partial_directory_injection_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_runtime_paths(config_dir=tmp_path / "config")
    with pytest.raises(ValueError):
        resolve_runtime_paths(cache_dir=tmp_path / "cache")
