"""Injectable wall and monotonic clocks for the resident Collector（PR #10）.

Responsibility: separate timestamping from scheduling so tests can drive
cadence without sleeping on the system clock. Non-goals: freshness policy,
provider I/O, or HTTP.

Inputs: none for system clocks; tests inject controllable clocks. Outputs:
UTC Unix seconds (wall) and monotonic seconds (scheduling). Units stay
seconds.

Contract: `docs/contracts/collector-service.md`. `freshness.Clock` is the
wall-clock protocol used by cache restore; this module adds monotonic
scheduling and a wakeup primitive.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol


class WallClock(Protocol):
    """UTC Unix-seconds clock for collected_at / generated_at / freshness."""

    def now(self) -> int:
        """Return UTC Unix seconds."""


class MonotonicClock(Protocol):
    """Monotonic seconds used only for interval, backoff, and deadlines."""

    def monotonic(self) -> float:
        """Return monotonic seconds. Not comparable to wall-clock Unix time."""


class Wakeup(Protocol):
    """Scheduler wait/notify. Production uses real time; tests use fake time."""

    def wait(self, timeout: float | None) -> None:
        """Block until notify, or until timeout monotonic seconds elapse."""

    def notify(self) -> None:
        """Wake every waiter. Used for settings apply, job completion, stop."""


class SystemWallClock:
    """Production wall clock. Tests must inject a fake instead of this class."""

    def now(self) -> int:
        return int(time.time())


class SystemMonotonicClock:
    """Production monotonic clock for scheduling."""

    def monotonic(self) -> float:
        return time.monotonic()


class ConditionWakeup:
    """threading.Condition waiter. timeout is real seconds.

    CONTRACT: notify 發生在 wait 之前也不可遺失（PR #10）。pending flag 補上
    「算完下一輪 sleep 到進入 wait」這段空窗。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._pending = False

    def wait(self, timeout: float | None) -> None:
        with self._cond:
            if self._pending:
                self._pending = False
                return
            self._cond.wait(timeout=timeout)
            self._pending = False

    def notify(self) -> None:
        with self._cond:
            self._pending = True
            self._cond.notify_all()
