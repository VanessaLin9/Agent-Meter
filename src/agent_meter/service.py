"""Headless desktop Collector entrypoint. No HTTP（PR #10）.

Responsibility: construct CollectorService from default runtime paths and
run until SIGINT/SIGTERM. Non-goals: bind address, FastAPI, or auto-start
login items.

Inputs: none. Outputs: process exit after bounded shutdown. Live collection
only happens for providers already enabled in the persisted settings file.
"""

from __future__ import annotations

import signal
import sys
import threading

from agent_meter.collector_service import CollectorService
from agent_meter.instance_lock import ServiceAlreadyRunningError, ServiceLockError
from agent_meter.paths import resolve_runtime_paths


def main() -> int:
    service = CollectorService(resolve_runtime_paths(), stderr=sys.stderr)
    stop = threading.Event()

    def _request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        service.start()
    except (ServiceAlreadyRunningError, ServiceLockError) as exc:
        sys.stderr.write(f"agent_meter.service: {exc}\n")
        return 1
    try:
        while not stop.wait(timeout=0.25):
            pass
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
