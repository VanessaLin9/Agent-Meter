"""Single-instance lock for one Collector service per runtime directory（PR #10）.

Responsibility: exclusive-lock the desktop state directory so a second
process cannot share cache/settings writers. Non-goals: HTTP, scheduling,
or reading provider credentials.

Inputs: RuntimePaths. Outputs: an acquired lock, or a sanitized error.
The lock file is created under config_dir; messages never include paths.

Contract: `docs/contracts/collector-service.md`. fcntl is the macOS/Linux
exclusive lock; this batch does not target Windows.
"""

from __future__ import annotations

import fcntl
import os
import stat

from agent_meter.paths import RuntimePaths
from agent_meter.private_files import (
    PrivateFileError,
    is_absent,
    prepare_private_dir,
    reject_unsafe_dir,
)

LOCK_FILE_MODE = 0o600


class ServiceAlreadyRunningError(Exception):
    """Another Collector instance already owns this runtime directory."""

    def __init__(self) -> None:
        super().__init__("Collector service is already running")


class ServiceLockError(Exception):
    """The runtime directory could not be locked. Do not echo OSError paths."""

    def __init__(self) -> None:
        super().__init__("Collector service could not lock the runtime directory")


class ServiceInstanceLock:
    """Exclusive flock on RuntimePaths.service_lock_file. Hold until release."""

    def __init__(self, paths: RuntimePaths) -> None:
        self._paths = paths
        self._fd: int | None = None

    def acquire(self) -> None:
        """Create the private config dir if needed and take LOCK_EX|LOCK_NB.

        SECURITY: 錯誤不得含 lock path 或 OSError 原文；可能含本機帳號目錄（PR #10）。
        """

        if self._fd is not None:
            return
        try:
            self._prepare_lock_dir()
            lock_path = self._paths.service_lock_file
            if lock_path.is_symlink():
                raise PrivateFileError()
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(
                lock_path,
                flags,
                LOCK_FILE_MODE,
            )
        except (PrivateFileError, OSError):
            raise ServiceLockError() from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError()
            os.fchmod(fd, LOCK_FILE_MODE)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ServiceAlreadyRunningError() from None
        except OSError:
            os.close(fd)
            raise ServiceLockError() from None
        self._fd = fd

    def _prepare_lock_dir(self) -> None:
        # SECURITY: 既有 config_dir 不得在 load() 前被 chmod 成 0700，否則
        # world-writable 目錄裡的 settings.json 會通過 private-mode 檢查並
        # 開始 live poll（PR #10）。缺目錄才建立；symlink／非目錄仍拒絕。
        config_dir = self._paths.config_dir
        if is_absent(config_dir):
            prepare_private_dir(config_dir)
            return
        reject_unsafe_dir(config_dir, require_private_mode=False)

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
