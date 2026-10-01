"""Atomic settings persistence with revision CAS（PR #8）.

Responsibility: bounded read, private directory/file modes, symlink refusal,
atomic replace, and compare-and-swap on revision. Non-goals: file watching,
runtime generation fencing, HTTP status codes, or snapshot cache I/O.

Inputs: RuntimePaths plus a SettingsWriteRequest. Outputs: Settings, or a
typed settings error whose message never includes the file bytes. Units:
revision is a non-negative integer; file size is bytes.

Contract: `docs/contracts/settings.md`. The desktop service owns when to
call load/save; this store always reads the current bytes and does not hot
reload a cached document by itself.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

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
from agent_meter.settings import (
    Settings,
    SettingsRevisionMismatch,
    SettingsValidationError,
    SettingsWriteRequest,
    dump_settings,
    first_install_settings,
    next_settings,
    parse_settings,
)

# CONTRACT: settings.json is tiny. A larger file is treated as damaged, not
# parsed incrementally.
MAX_SETTINGS_BYTES = 16 * 1024


class SettingsConfigError(Exception):
    """Damaged, unreadable, or unsafe settings file. Do not overwrite it."""

    code = "config_error"

    def __init__(self) -> None:
        super().__init__("Settings could not be read")


class SettingsRevisionConflict(Exception):
    """CAS expected_revision does not match the document on disk."""

    code = "revision_conflict"

    def __init__(self) -> None:
        super().__init__("Settings revision does not match")


class SettingsSaveError(Exception):
    """Atomic replace failed. The previous document must remain in place."""

    code = "save_failed"

    def __init__(self) -> None:
        super().__init__("Settings could not be saved")


def _os_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


class SettingsStore:
    """Persist Settings under RuntimePaths.config_dir / settings.json."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        replace: ReplaceFn | None = None,
    ) -> None:
        self._paths = paths
        self._replace = replace if replace is not None else _os_replace
        self._lock = threading.Lock()

    def load(self) -> Settings:
        """Return persisted settings, or first-install defaults when the file is absent."""

        path = self._paths.settings_file
        try:
            if is_absent(path):
                # FALLBACK: missing file is first install, not all-providers-on
                # （PR #8）。Loose directory mode is repaired on the first
                # successful save; a symlink config dir is never a blank slate.
                self._reject_dir_if_present(require_private_mode=False)
                return first_install_settings()
            self._reject_dir_if_present(require_private_mode=True)
            reject_unsafe_file(path)
            raw = read_bounded_nofollow(path, MAX_SETTINGS_BYTES)
            payload = parse_json_object(raw)
            return parse_settings(payload)
        except SettingsValidationError:
            raise SettingsConfigError() from None
        except SettingsConfigError:
            raise
        except (PrivateFileError, OSError):
            raise SettingsConfigError() from None

    def save(self, request: SettingsWriteRequest) -> Settings:
        """Validate, CAS, persist atomically, then return the new document."""

        # CONTRACT: persist succeeds before the caller may apply runtime
        # （PR #8）。A failed save must leave the previous file and revision
        # untouched.
        with self._lock:
            current = self.load()
            try:
                document = next_settings(current, request)
            except SettingsRevisionMismatch:
                raise SettingsRevisionConflict() from None
            payload = json.dumps(dump_settings(document), indent=2, ensure_ascii=True) + "\n"
            try:
                prepare_private_dir(self._paths.config_dir)
                atomic_replace(
                    self._paths.settings_file,
                    payload,
                    replace=self._replace,
                )
            except SettingsSaveError:
                raise
            except (PrivateFileError, OSError):
                raise SettingsSaveError() from None
            return document

    def _reject_dir_if_present(self, *, require_private_mode: bool) -> None:
        config_dir = self._paths.config_dir
        if is_absent(config_dir):
            return
        reject_unsafe_dir(config_dir, require_private_mode=require_private_mode)
