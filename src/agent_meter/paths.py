"""Runtime directories for the desktop Collector service（PR #8）.

Responsibility: compose checkout-external config and cache paths.
Non-goals: creating directories, reading settings, or storing credentials.

Inputs: an injectable home or explicit config/cache directories. Outputs:
immutable RuntimePaths. Tests inject a temp dir; production uses the user
Library locations on macOS.

Contract: `docs/contracts/settings.md`. No module here may write into the
Git checkout or read provider-owned session files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# SECURITY: runtime files stay under the user Library, never the repo（PR #8）。
APP_DIR_NAME = "Agent Meter"
SETTINGS_FILE_NAME = "settings.json"
SNAPSHOT_CACHE_DIR_NAME = "snapshots"
MAILBOX_DIR_NAME = "mailbox"


@dataclass(frozen=True)
class RuntimePaths:
    """Checkout-external directories for settings, cache, and mailbox."""

    config_dir: Path
    cache_dir: Path

    @property
    def settings_file(self) -> Path:
        return self.config_dir / SETTINGS_FILE_NAME

    @property
    def snapshot_cache_dir(self) -> Path:
        return self.cache_dir / SNAPSHOT_CACHE_DIR_NAME

    @property
    def mailbox_dir(self) -> Path:
        return self.cache_dir / MAILBOX_DIR_NAME


def resolve_runtime_paths(
    *,
    home: Path | None = None,
    config_dir: Path | None = None,
    cache_dir: Path | None = None,
) -> RuntimePaths:
    """Build macOS Library paths, or use both injected directories in tests."""

    if (config_dir is None) != (cache_dir is None):
        raise ValueError("config_dir and cache_dir must both be set or both omitted")
    if config_dir is not None and cache_dir is not None:
        return RuntimePaths(config_dir=config_dir, cache_dir=cache_dir)
    root = home if home is not None else Path.home()
    return RuntimePaths(
        config_dir=root / "Library" / "Application Support" / APP_DIR_NAME,
        cache_dir=root / "Library" / "Caches" / APP_DIR_NAME,
    )
