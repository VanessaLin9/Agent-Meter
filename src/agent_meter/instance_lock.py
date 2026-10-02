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

from agent_meter.paths import RuntimePaths
from agent_meter.private_files import PrivateFileError, prepare_private_dir

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
            prepare_private_dir(self._paths.config_dir)
            fd = os.open(
                self._paths.service_lock_file,
                os.O_CREAT | os.O_RDWR,
                LOCK_FILE_MODE,
            )
        except (PrivateFileError, OSError):
            raise ServiceLockError() from None
        try:
            os.fchmod(fd, LOCK_FILE_MODE)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise ServiceAlreadyRunningError() from None
        except OSError:
            os.close(fd)
            raise ServiceLockError() from None
        self._fd = fd

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
