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
import stat
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from agent_meter.paths import RuntimePaths
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
_DIR_MODE = 0o700
_FILE_MODE = 0o600

type ReplaceFn = Callable[[Path, Path], None]


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


def _default_replace(source: Path, destination: Path) -> None:
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
        self._replace = replace if replace is not None else _default_replace
        self._lock = threading.Lock()

    def load(self) -> Settings:
        """Return persisted settings, or first-install defaults when the file is absent."""

        path = self._paths.settings_file
        try:
            if _is_absent(path):
                # FALLBACK: missing file is first install, not all-providers-on
                # （PR #8）。Loose directory mode is repaired on the first
                # successful save; a symlink config dir is never a blank slate.
                self._reject_dir_if_present(require_private_mode=False)
                return first_install_settings()
            self._reject_dir_if_present(require_private_mode=True)
            _reject_unsafe_file(path)
            raw = _read_bounded_nofollow(path, MAX_SETTINGS_BYTES)
            payload = _parse_json_object(raw)
            return parse_settings(payload)
        except SettingsValidationError:
            raise SettingsConfigError() from None
        except SettingsConfigError:
            raise
        except OSError:
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
                self._prepare_config_dir()
                _atomic_replace(
                    self._paths.settings_file,
                    payload,
                    replace=self._replace,
                )
            except SettingsSaveError:
                raise
            except OSError:
                raise SettingsSaveError() from None
            return document

    def _reject_dir_if_present(self, *, require_private_mode: bool) -> None:
        config_dir = self._paths.config_dir
        if _is_absent(config_dir):
            return
        _reject_unsafe_dir(config_dir, require_private_mode=require_private_mode)

    def _prepare_config_dir(self) -> None:
        config_dir = self._paths.config_dir
        if config_dir.is_symlink():
            raise SettingsSaveError()
        if config_dir.exists() and not config_dir.is_dir():
            raise SettingsSaveError()
        config_dir.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        if config_dir.is_symlink():
            raise SettingsSaveError()
        os.chmod(config_dir, _DIR_MODE)


def _is_absent(path: Path) -> bool:
    return not path.exists() and not path.is_symlink()


def _reject_unsafe_dir(path: Path, *, require_private_mode: bool) -> None:
    try:
        st = path.lstat()
    except OSError:
        raise SettingsConfigError() from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise SettingsConfigError()
    if require_private_mode and stat.S_IMODE(st.st_mode) != _DIR_MODE:
        raise SettingsConfigError()


def _reject_unsafe_file(path: Path) -> None:
    try:
        st = path.lstat()
    except OSError:
        raise SettingsConfigError() from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise SettingsConfigError()
    if stat.S_IMODE(st.st_mode) != _FILE_MODE:
        raise SettingsConfigError()


def _read_bounded_nofollow(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise SettingsConfigError()
    return data


def _parse_json_object(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8")
        payload: object = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SettingsConfigError() from None
    if not isinstance(payload, dict):
        raise SettingsConfigError()
    return payload


def _atomic_replace(path: Path, payload: str, *, replace: ReplaceFn) -> None:
    # SECURITY: temp+replace so readers never see a partial JSON document.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.chmod(tmp_path, _FILE_MODE)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace(tmp_path, path)
        os.chmod(path, _FILE_MODE)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise SettingsSaveError() from None
