"""Codex app-server JSON-RPC transport（PR #5）。

Responsibility: spawn `codex app-server --stdio`, exchange JSONL JSON-RPC
messages, pair responses by `id`, and always reap the child process.
Non-goals: quota mapping, cache, HTTP, or reading Codex credentials.

Inputs: argv, allowlisted env, monotonic deadline. Outputs: the JSON-RPC
`result` object, or a typed collection failure. stdout of the child is the
protocol channel; child stderr is discarded.

Contract: `docs/providers/codex.md` and `docs/contracts/provider-adapter.md`.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from typing import IO, NoReturn, TextIO

from agent_meter.cache import ProviderCollectionFailure
from agent_meter.models import ErrorCategory

CODEX_PROVIDER_ID = "codex"
CODEX_SOURCE = "codex_app_server"
DEFAULT_DEADLINE_SECONDS = 10.0
DEFAULT_MAX_LINE_BYTES = 1_048_576
_TERMINATE_GRACE_SECONDS = 1.0
_INITIALIZE_ID = 1
_RATE_LIMITS_ID = 3
_CLIENT_INFO = {
    "name": "agent-meter",
    "title": "Agent Meter",
    "version": "0.1.0",
}
_ALLOWED_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TZ",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "CODEX_HOME",
    }
)

# SECURITY: never copy parent env wholesale. Tokens such as OPENAI_API_KEY
# stay out of the child even if the Collector process has them.


class _RpcTimeout(Exception):
    """Deadline elapsed while waiting for JSON-RPC I/O."""


class _ProtocolError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _LineReader:
    """Bounded JSONL reader with a monotonic deadline."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._buf = bytearray()
        self._eof = False

    def readline(self, *, deadline: float, max_bytes: int) -> bytes | None:
        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line = bytes(self._buf[:newline])
                del self._buf[: newline + 1]
                return line
            if len(self._buf) > max_bytes:
                raise _ProtocolError("line_too_large")
            if self._eof:
                if not self._buf:
                    return None
                line = bytes(self._buf)
                self._buf.clear()
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _RpcTimeout()
            ready, _, _ = select.select([self._fd], [], [], remaining)
            if not ready:
                raise _RpcTimeout()
            chunk = os.read(self._fd, 4096)
            if not chunk:
                self._eof = True
                continue
            self._buf.extend(chunk)


def allowlisted_environ(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy only allowlisted keys from the parent environment."""

    env = {key: os.environ[key] for key in _ALLOWED_ENV_KEYS if key in os.environ}
    if overrides:
        env.update(overrides)
    return env


def default_codex_command(env: Mapping[str, str]) -> list[str] | ProviderCollectionFailure:
    """Resolve `codex app-server --stdio` from PATH. Missing binary is not_installed."""

    path = env.get("PATH")
    which = _which("codex", path)
    if which is None:
        return _failure(
            "not_installed",
            "not_installed",
            "Codex CLI is not installed",
            retryable=False,
        )
    return [which, "app-server", "--stdio"]


def read_rate_limits_result(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    deadline: float,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
    stderr: TextIO | None = None,
) -> dict[str, object] | ProviderCollectionFailure:
    """Run the JSON-RPC handshake and return the rate-limit result object."""

    try:
        proc = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            start_new_session=True,
            bufsize=0,
        )
    except FileNotFoundError:
        return _failure(
            "not_installed",
            "not_installed",
            "Codex CLI is not installed",
            retryable=False,
        )
    except OSError:
        return _failure(
            "upstream",
            "spawn_failed",
            "Failed to start Codex app-server",
            retryable=True,
        )

    assert proc.stdin is not None
    assert proc.stdout is not None
    reader = _LineReader(proc.stdout.fileno())
    try:
        return _handshake(
            proc,
            reader,
            deadline=deadline,
            max_line_bytes=max_line_bytes,
            stderr=stderr,
        )
    except _RpcTimeout:
        return _failure(
            "timeout",
            "timeout",
            "Codex app-server exceeded the deadline",
            retryable=True,
        )
    except _ProtocolError as exc:
        return _protocol_failure(exc.code)
    except BrokenPipeError:
        return _failure(
            "upstream",
            "broken_pipe",
            "Codex app-server closed the protocol pipe",
            retryable=True,
        )
    finally:
        _stop_process(proc)


def _handshake(
    proc: subprocess.Popen[bytes],
    reader: _LineReader,
    *,
    deadline: float,
    max_line_bytes: int,
    stderr: TextIO | None,
) -> dict[str, object]:
    stdin = proc.stdin
    assert stdin is not None
    _write_message(
        stdin,
        {
            "method": "initialize",
            "id": _INITIALIZE_ID,
            "params": {"clientInfo": _CLIENT_INFO},
        },
    )
    _wait_result(
        proc,
        reader,
        request_id=_INITIALIZE_ID,
        deadline=deadline,
        max_line_bytes=max_line_bytes,
        stderr=stderr,
    )
    _write_message(stdin, {"method": "initialized"})
    _write_message(
        stdin,
        {"method": "account/rateLimits/read", "id": _RATE_LIMITS_ID},
    )
    result = _wait_result(
        proc,
        reader,
        request_id=_RATE_LIMITS_ID,
        deadline=deadline,
        max_line_bytes=max_line_bytes,
        stderr=stderr,
    )
    if not isinstance(result, dict):
        raise _ProtocolError("missing_result")
    return result


def _wait_result(
    proc: subprocess.Popen[bytes],
    reader: _LineReader,
    *,
    request_id: int,
    deadline: float,
    max_line_bytes: int,
    stderr: TextIO | None,
) -> object:
    while True:
        message = _read_message(reader, deadline=deadline, max_line_bytes=max_line_bytes)
        if message is None:
            if proc.poll() not in (None, 0):
                raise _ProtocolError("process_exited")
            raise _ProtocolError("eof")
        if "id" not in message:
            # CONTRACT: pair by request id. Notifications are ignored and may
            # be observed on stderr without copying params（PR #5）。
            if stderr is not None:
                method = message.get("method")
                label = method if isinstance(method, str) and method.isascii() else "notification"
                stderr.write(f"codex: ignored JSON-RPC {label}\n")
            continue
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise _rpc_error(message["error"])
        if "result" not in message:
            raise _ProtocolError("missing_result")
        return message["result"]


def _read_message(
    reader: _LineReader, *, deadline: float, max_line_bytes: int
) -> dict[str, object] | None:
    while True:
        raw = reader.readline(deadline=deadline, max_bytes=max_line_bytes)
        if raw is None:
            return None
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            text = stripped.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ProtocolError("malformed_json") from exc
        decoder = json.JSONDecoder(parse_constant=_reject_nonstandard_constant)
        try:
            parsed, end = decoder.raw_decode(text)
        except (json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise _ProtocolError("malformed_json") from exc
        if text[end:].strip():
            raise _ProtocolError("malformed_json")
        if not isinstance(parsed, dict):
            raise _ProtocolError("malformed_json")
        value: dict[str, object] = parsed
        return value


def _write_message(stdin: IO[bytes], payload: dict[str, object]) -> None:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    stdin.write(raw)
    stdin.write(b"\n")
    stdin.flush()


def _rpc_error(error: object) -> _ProtocolError:
    # SECURITY: never copy upstream error.message; it may include account or
    # session text. Map to a stable category from sanitized signals only.
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and _looks_unauthenticated(message):
            return _ProtocolError("not_authenticated")
    return _ProtocolError("upstream_error")


def _looks_unauthenticated(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "unauthenticated",
            "unauthorized",
            "not authenticated",
            "not logged in",
            "not signed in",
            "login required",
        )
    )


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    # CONTRACT: timeout and success both reap the child. SIGTERM first, then
    # SIGKILL, including the process group so app-server helpers cannot linger.
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    if proc.poll() is not None:
        _close_pipes(proc)
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        _close_pipes(proc)
        return
    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    _close_pipes(proc)


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _which(name: str, path: str | None) -> str | None:
    from shutil import which

    return which(name, path=path)


def _protocol_failure(code: str) -> ProviderCollectionFailure:
    mapping: dict[str, tuple[ErrorCategory, str, bool]] = {
        "eof": ("malformed_response", "Codex app-server closed stdout before a result", False),
        "malformed_json": (
            "malformed_response",
            "Codex app-server stdout is not valid JSON-RPC",
            False,
        ),
        "line_too_large": (
            "malformed_response",
            "Codex app-server stdout line exceeds the size limit",
            False,
        ),
        "missing_result": (
            "malformed_response",
            "Codex JSON-RPC response is missing result",
            False,
        ),
        "process_exited": ("upstream", "Codex app-server exited before a result", True),
        "not_authenticated": (
            "not_authenticated",
            "Codex app-server has no usable login state",
            False,
        ),
        "upstream_error": ("upstream", "Codex app-server returned a JSON-RPC error", True),
    }
    category, message, retryable = mapping.get(
        code,
        ("malformed_response", "Codex app-server returned a malformed protocol message", False),
    )
    return _failure(category, code, message, retryable=retryable)


def _failure(
    category: ErrorCategory,
    code: str,
    message: str,
    *,
    retryable: bool,
) -> ProviderCollectionFailure:
    return ProviderCollectionFailure(
        provider_id=CODEX_PROVIDER_ID,
        source=CODEX_SOURCE,
        category=category,
        message=message,
        retryable=retryable,
        code=code,
    )


def _reject_nonstandard_constant(_literal: str) -> NoReturn:
    raise ValueError("non-standard JSON constant")
