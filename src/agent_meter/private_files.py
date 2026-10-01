"""Private directory/file helpers for checkout-external runtime files.

Responsibility: 0700/0600 modes, symlink refusal, bounded read, and atomic
replace. Non-goals: settings CAS, snapshot schema, HTTP.

Inputs: paths and UTF-8 payloads. Outputs: bytes or a replaced file. Units:
file size is bytes.

Contract: `docs/contracts/settings.md` and `docs/contracts/cache.md`. Callers
map failures to their own sanitized errors and must not echo file bytes.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600

type ReplaceFn = Callable[[Path, Path], None]


class PrivateFileError(Exception):
    """Unsafe path, oversize, or unreadable private file. Do not log the bytes."""


def is_absent(path: Path) -> bool:
    return not path.exists() and not path.is_symlink()


def reject_unsafe_dir(path: Path, *, require_private_mode: bool) -> None:
    try:
        st = path.lstat()
    except OSError:
        raise PrivateFileError() from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise PrivateFileError()
    if require_private_mode and stat.S_IMODE(st.st_mode) != DIR_MODE:
        raise PrivateFileError()


def reject_unsafe_file(path: Path) -> None:
    try:
        st = path.lstat()
    except OSError:
        raise PrivateFileError() from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise PrivateFileError()
    if stat.S_IMODE(st.st_mode) != FILE_MODE:
        raise PrivateFileError()


def prepare_private_dir(path: Path) -> None:
    if path.is_symlink():
        raise PrivateFileError()
    if path.exists() and not path.is_dir():
        raise PrivateFileError()
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    if path.is_symlink():
        raise PrivateFileError()
    os.chmod(path, DIR_MODE)


def read_bounded_nofollow(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise PrivateFileError()
    return data


def parse_json_object(raw: bytes) -> object:
    try:
        payload: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PrivateFileError() from None
    if not isinstance(payload, dict):
        raise PrivateFileError()
    return payload


def atomic_replace(path: Path, payload: str, *, replace: ReplaceFn) -> None:
    # SECURITY: temp+replace so readers never see a partial JSON document.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.chmod(tmp_path, FILE_MODE)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # CONTRACT: chmod the temp file before replace（PR #8）。replace 後再
        # chmod 會在磁碟已提交時仍回失敗，讓 runtime 與 disk 分裂。
        replace(tmp_path, path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
