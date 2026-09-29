"""One-shot provider orchestration（PR #7）。

Responsibility: call configured adapters independently, merge typed results
with domain policy, and write a gitignored schema-valid `usage.json`.
Non-goals: scheduler, FastAPI, persistent cache restore, or ESP32.

Inputs: collect callables (injected in tests), an injected clock value as UTC
Unix seconds, and an output path. Outputs: a validated UsageSnapshot and the
same payload on stdout / disk. Retry storms are out of scope; each provider
is attempted once.

Contract: `docs/contracts/usage-api.md` and
`docs/contracts/provider-adapter.md`. Live Codex/Cursor access is opt-in.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Protocol, TextIO

from agent_meter.aggregation import build_snapshot
from agent_meter.cache import (
    ProviderCollectionFailure,
    ProviderCollectionSuccess,
    merge_collection_results,
)
from agent_meter.freshness import StaleSettings
from agent_meter.models import (
    ErrorCategory,
    ProviderSnapshot,
    UsageSnapshot,
    dump_usage_snapshot,
)
from agent_meter.providers.claude import (
    CLAUDE_PROVIDER_ID,
    CLAUDE_SOURCE,
    DEFAULT_MAX_STDIN_BYTES,
    collect_statusline_bytes,
)
from agent_meter.providers.codex import collect as collect_codex_live
from agent_meter.providers.codex_rpc import (
    CODEX_PROVIDER_ID,
    CODEX_SOURCE,
    DEFAULT_DEADLINE_SECONDS,
)
from agent_meter.providers.cursor import collect as collect_cursor_live
from agent_meter.providers.cursor_rpc import CURSOR_PROVIDER_ID, CURSOR_SOURCE

CONFIGURED_PROVIDER_IDS: tuple[str, ...] = (
    CLAUDE_PROVIDER_ID,
    CODEX_PROVIDER_ID,
    CURSOR_PROVIDER_ID,
)
DEFAULT_OUTPUT_PATH = Path("usage.json")

_PROVIDER_SOURCES: dict[str, str] = {
    CLAUDE_PROVIDER_ID: CLAUDE_SOURCE,
    CODEX_PROVIDER_ID: CODEX_SOURCE,
    CURSOR_PROVIDER_ID: CURSOR_SOURCE,
}

# SECURITY: live Codex/Cursor access is opt-in. Default CLI must not spawn
# app-server, read Cursor session state, or wait on Claude stdin（PR #7）。


class ProviderCollector(Protocol):
    """One provider collect callable. Tests inject fakes; live CLI supplies adapters."""

    def __call__(
        self, *, now: int, deadline_seconds: float
    ) -> ProviderCollectionSuccess | ProviderCollectionFailure:
        """Return a typed result. Must not raise across the orchestrator boundary."""


def collect_snapshot(
    collectors: Mapping[str, ProviderCollector],
    *,
    now: int,
    settings: StaleSettings | None = None,
    previous: Mapping[str, ProviderSnapshot] | None = None,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    stderr: TextIO | None = None,
) -> UsageSnapshot:
    """Collect each provider independently, then aggregate. One failure stays local."""

    if not collectors:
        raise ValueError("collectors must not be empty")
    resolved_settings = StaleSettings() if settings is None else settings
    results: list[ProviderCollectionSuccess | ProviderCollectionFailure] = []
    for provider_id in _provider_order(collectors):
        result = _invoke_collector(
            provider_id,
            collectors[provider_id],
            now=now,
            deadline_seconds=deadline_seconds,
        )
        _diagnose(stderr, provider_id, result)
        results.append(result)
    merged = merge_collection_results(previous or {}, results, settings=resolved_settings)
    # CONTRACT: 設定的 provider 都要出現在 snapshot；空 map 不能假造 ok（PR #7）。
    return build_snapshot(merged, now=now)


def write_usage_snapshot(path: Path, snapshot: UsageSnapshot) -> None:
    """Atomically replace `path` with schema-valid JSON. Do not log the payload."""

    # SECURITY: 只寫 normalized snapshot。temp+replace 避免 reader 看到半份 JSON（PR #7）。
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dump_usage_snapshot(snapshot), indent=2, ensure_ascii=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def build_live_collectors(
    *,
    claude_statusline: Path | None,
    collect_codex: ProviderCollector | None = None,
    collect_cursor: ProviderCollector | None = None,
    stdin: BinaryIO | None = None,
    stderr: TextIO | None = None,
) -> dict[str, ProviderCollector]:
    """Wire the three v0.1 adapters. Tests inject Codex/Cursor callables."""

    stdin_stream = sys.stdin.buffer if stdin is None else stdin
    diagnostic = sys.stderr if stderr is None else stderr
    codex = collect_codex if collect_codex is not None else _bind_codex(diagnostic)
    cursor = collect_cursor if collect_cursor is not None else _bind_cursor()
    return {
        CLAUDE_PROVIDER_ID: _bind_claude(claude_statusline, stdin_stream),
        CODEX_PROVIDER_ID: codex,
        CURSOR_PROVIDER_ID: cursor,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry for `python -m agent_meter`."""

    parser = argparse.ArgumentParser(prog="agent_meter")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Collect from local provider sessions. Required; default is offline.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Gitignored snapshot path (default: usage.json).",
    )
    parser.add_argument(
        "--claude-statusline",
        type=Path,
        default=None,
        help="Claude Code status-line JSON file, or - for stdin.",
    )
    args = parser.parse_args(argv)
    if not args.live:
        sys.stderr.write("agent_meter: live collection is opt-in; pass --live\n")
        return 2

    # SECURITY: say what --live will do before any session read or network call（PR #7）。
    sys.stderr.write(
        "agent_meter: Codex spawns app-server; Cursor reads the local session and "
        "posts GetCurrentPeriodUsage without refreshing the token; Claude parses a "
        "local status-line JSON file when provided\n"
    )
    collectors = build_live_collectors(
        claude_statusline=args.claude_statusline,
        stderr=sys.stderr,
    )
    snapshot = collect_snapshot(collectors, now=_unix_now(), stderr=sys.stderr)
    stdout_payload = json.dumps(
        dump_usage_snapshot(snapshot), separators=(",", ":"), ensure_ascii=True
    )
    try:
        write_usage_snapshot(args.output, snapshot)
    except OSError:
        # SECURITY: 寫檔失敗不回顯 path；可能含本機帳號目錄（PR #7）。
        sys.stderr.write("agent_meter: failed to write usage snapshot\n")
        sys.stdout.write(stdout_payload)
        sys.stdout.write("\n")
        return 1
    sys.stderr.write(f"agent_meter: wrote {args.output.name} status={snapshot.status}\n")
    sys.stdout.write(stdout_payload)
    sys.stdout.write("\n")
    return 0


def _unix_now() -> int:
    return int(time.time())


def _provider_order(collectors: Mapping[str, ProviderCollector]) -> tuple[str, ...]:
    known = tuple(
        provider_id for provider_id in CONFIGURED_PROVIDER_IDS if provider_id in collectors
    )
    extras = tuple(
        provider_id for provider_id in collectors if provider_id not in CONFIGURED_PROVIDER_IDS
    )
    return known + extras


def _invoke_collector(
    provider_id: str,
    collector: ProviderCollector,
    *,
    now: int,
    deadline_seconds: float,
) -> ProviderCollectionSuccess | ProviderCollectionFailure:
    try:
        result = collector(now=now, deadline_seconds=deadline_seconds)
    except TimeoutError:
        # SECURITY: 不得 stringify TimeoutError；message 可能含 planted secret（PR #7）。
        return _isolated_failure(
            provider_id,
            category="timeout",
            message="Provider collection timed out",
            retryable=True,
            code="deadline_exceeded",
        )
    except Exception:
        # FALLBACK: unexpected throw 只讓該 provider 失敗，不得中斷其他 collect（PR #7）。
        # SECURITY: 不把 exception 或 traceback locals 寫進 snapshot／stderr（PR #7）。
        return _isolated_failure(
            provider_id,
            category="internal",
            message="Provider collection failed",
            retryable=True,
            code="collector_exception",
        )
    if not isinstance(result, (ProviderCollectionSuccess, ProviderCollectionFailure)):
        # FALLBACK: 非 typed result 當 malformed，避免 0% 或 raw dict 進 snapshot（PR #7）。
        return _isolated_failure(
            provider_id,
            category="malformed_response",
            message="Provider collector returned an unusable result",
            retryable=False,
            code="untyped_result",
        )
    if result.provider_id != provider_id:
        # CONTRACT: collector 不能把結果寫進別的 provider key（PR #7）。
        return _isolated_failure(
            provider_id,
            category="internal",
            message="Provider result id mismatch",
            retryable=False,
            code="provider_id_mismatch",
        )
    return result


def _isolated_failure(
    provider_id: str,
    *,
    category: ErrorCategory,
    message: str,
    retryable: bool,
    code: str,
) -> ProviderCollectionFailure:
    return ProviderCollectionFailure(
        provider_id=provider_id,
        source=_source_for(provider_id),
        category=category,
        message=message,
        retryable=retryable,
        code=code,
    )


def _source_for(provider_id: str) -> str:
    return _PROVIDER_SOURCES.get(provider_id, "collector")


def _diagnose(
    stderr: TextIO | None,
    provider_id: str,
    result: ProviderCollectionSuccess | ProviderCollectionFailure,
) -> None:
    if stderr is None:
        return
    if isinstance(result, ProviderCollectionSuccess):
        stderr.write(f"{provider_id}: ok\n")
        return
    stderr.write(f"{provider_id}: {result.category}: {result.message}\n")


def _bind_claude(path: Path | None, stdin: BinaryIO) -> ProviderCollector:
    def collect(
        *, now: int, deadline_seconds: float
    ) -> ProviderCollectionSuccess | ProviderCollectionFailure:
        del deadline_seconds
        return _collect_claude_statusline(path, now=now, stdin=stdin)

    return collect


def _bind_codex(stderr: TextIO) -> ProviderCollector:
    def collect(
        *, now: int, deadline_seconds: float
    ) -> ProviderCollectionSuccess | ProviderCollectionFailure:
        return collect_codex_live(now=now, deadline_seconds=deadline_seconds, stderr=stderr)

    return collect


def _bind_cursor() -> ProviderCollector:
    def collect(
        *, now: int, deadline_seconds: float
    ) -> ProviderCollectionSuccess | ProviderCollectionFailure:
        return collect_cursor_live(now=now, deadline_seconds=deadline_seconds)

    return collect


def _collect_claude_statusline(
    path: Path | None,
    *,
    now: int,
    stdin: BinaryIO,
    max_bytes: int = DEFAULT_MAX_STDIN_BYTES,
) -> ProviderCollectionSuccess | ProviderCollectionFailure:
    if path is None:
        # FALLBACK: Claude 沒給 status-line 檔是 unavailable，不阻擋 Codex／Cursor（PR #7）。
        return ProviderCollectionFailure(
            provider_id=CLAUDE_PROVIDER_ID,
            source=CLAUDE_SOURCE,
            category="not_configured",
            message="Claude status-line input was not provided",
            retryable=False,
            code="missing_statusline",
        )
    if str(path) == "-":
        raw = stdin.read(max_bytes + 1)
        return _claude_from_bytes(raw, now=now, max_bytes=max_bytes)
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        # SECURITY: 不把 filesystem path 寫進 error；可能含本機帳號目錄（PR #7）。
        return ProviderCollectionFailure(
            provider_id=CLAUDE_PROVIDER_ID,
            source=CLAUDE_SOURCE,
            category="not_configured",
            message="Claude status-line input could not be read",
            retryable=False,
            code="unreadable_statusline",
        )
    return _claude_from_bytes(raw, now=now, max_bytes=max_bytes)


def _claude_from_bytes(
    raw: bytes, *, now: int, max_bytes: int
) -> ProviderCollectionSuccess | ProviderCollectionFailure:
    if len(raw) > max_bytes:
        return ProviderCollectionFailure(
            provider_id=CLAUDE_PROVIDER_ID,
            source=CLAUDE_SOURCE,
            category="malformed_response",
            message="Claude status-line input exceeds the size limit",
            retryable=False,
            code="input_too_large",
        )
    return collect_statusline_bytes(raw, now=now)


if __name__ == "__main__":
    raise SystemExit(main())
