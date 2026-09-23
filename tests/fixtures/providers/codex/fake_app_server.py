"""Fake Codex app-server for offline adapter tests.

Reads JSON-RPC JSONL from stdin and writes canned JSONL to stdout.
Scenario is selected by argv; this process never talks to a live Codex service.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

FAKE_SECRET = "PLANTED_PROVIDER_TOKEN_VALUE_DO_NOT_EMIT"
HAPPY_RESULT = json.loads(
    (Path(__file__).resolve().parent / "happy.json").read_text(encoding="utf-8")
)


def _write(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _sleep_forever() -> None:
    while True:
        time.sleep(0.05)


def _handle_request(scenario: str, message: dict[str, object]) -> str:
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        if scenario == "notification_first":
            _write(
                {
                    "method": "codex/event",
                    "params": {"token": FAKE_SECRET},
                }
            )
        if scenario == "wrong_id_first":
            _write({"id": 99, "result": {"unexpected": FAKE_SECRET}})
        if scenario == "eof_after_initialize":
            _write({"id": request_id, "result": {"ok": True}})
            sys.stdout.flush()
            os.close(sys.stdout.fileno())
            for _unused in sys.stdin.buffer:
                pass
            return "done"
        _write({"id": request_id, "result": {"ok": True}})
        return scenario

    if method == "initialized":
        return scenario

    if method == "account/rateLimits/read":
        if scenario == "notification_first":
            _write({"method": "codex/progress", "params": {"secret": FAKE_SECRET}})
        if scenario == "non_json":
            sys.stdout.write(f"debug: {FAKE_SECRET}\n")
            sys.stdout.flush()
            return "done"
        if scenario == "missing_result":
            _write({"id": request_id})
            return "done"
        if scenario == "jsonrpc_error":
            _write(
                {
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": f"backend failed {FAKE_SECRET}",
                    },
                }
            )
            return "done"
        if scenario == "unauthenticated":
            _write(
                {
                    "id": request_id,
                    "error": {
                        "code": -32001,
                        "message": f"UNAUTHENTICATED {FAKE_SECRET}",
                    },
                }
            )
            return "done"
        _write({"id": request_id, "result": HAPPY_RESULT})
        return "done"

    return scenario


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario")
    parser.add_argument("--pid-file")
    args = parser.parse_args(argv)

    if args.pid_file:
        Path(args.pid_file).write_text(str(os.getpid()), encoding="utf-8")

    if args.scenario == "timeout":
        _sleep_forever()
        return 0
    if args.scenario == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _sleep_forever()
        return 0
    if args.scenario == "exit_1":
        sys.stdin.readline()
        return 1

    scenario = args.scenario
    if scenario == "fail_if_secret_in_env":
        for key, value in os.environ.items():
            if FAKE_SECRET in key or FAKE_SECRET in value:
                sys.stdout.write("inherited-secret\n")
                sys.stdout.flush()
                return 0
        scenario = "happy"

    for raw in sys.stdin:
        stripped = raw.strip()
        if not stripped:
            continue
        message = json.loads(stripped)
        if not isinstance(message, dict):
            return 1
        scenario = _handle_request(scenario, message)
        if scenario == "done":
            break

    for _ in sys.stdin:
        break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
