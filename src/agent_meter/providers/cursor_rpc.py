"""Cursor unofficial Connect RPC transport（PR #6）。

Responsibility: read Cursor's existing local session and POST
`GetCurrentPeriodUsage`. Non-goals: quota mapping, token refresh, cache, and
changing Cursor settings or login state.

Inputs: a state directory, an optional client version, and a monotonic
deadline. The access token, machine ids, and checksum exist only while the
request is built and sent. Outputs: response status plus body bytes, or a
typed collection failure. Failure messages never include those values.

Contract: `docs/providers/cursor.md` and `docs/contracts/provider-adapter.md`.
Retry and last-good fallback belong to the orchestrator.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol
from urllib.parse import quote

from agent_meter.cache import ProviderCollectionFailure
from agent_meter.models import ErrorCategory

CURSOR_PROVIDER_ID = "cursor"
CURSOR_SOURCE = "cursor_dashboard_connect_rpc"
DEFAULT_DEADLINE_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 1_048_576
_PERIOD_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
_TOKEN_KEY = "cursorAuth/accessToken"
_MACHINE_ID_KEY = "telemetry.machineId"
_MAC_MACHINE_ID_KEY = "telemetry.macMachineId"

_FAILURE_MESSAGES = {
    "missing_local_state": "Cursor local state is not available",
    "unreadable_local_state": "Cursor local state could not be read",
    "missing_login": "Cursor login state is not available",
    "missing_client_version": "Cursor client version is not available",
    "deadline_exceeded": "Cursor period-usage request exceeded the deadline",
    "connection_failed": "Cursor period-usage request could not connect",
    "token_rejected": "Cursor rejected the local login state",
    "upstream_http_error": "Cursor period-usage request was not successful",
    "response_too_large": "Cursor period-usage response exceeds the size limit",
    "malformed_json": "Cursor period-usage response is not valid JSON",
}

_RETRYABLE = {
    "deadline_exceeded": True,
    "connection_failed": True,
}


class CursorTransport(Protocol):
    """Send one prepared request. Implementations must not log the request."""

    def __call__(
        self, request: PreparedRequest, *, deadline_seconds: float
    ) -> TransportResponse | ProviderCollectionFailure: ...


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: bytes


@dataclass(frozen=True)
class PreparedRequest:
    """Connect RPC request. Repr stays redacted because headers hold the token."""

    url: str
    body: bytes
    headers: tuple[tuple[str, str], ...]

    def __repr__(self) -> str:
        return "PreparedRequest(redacted)"


@dataclass(frozen=True)
class _SessionMaterial:
    """Values needed to build one request. Repr stays redacted."""

    token: str
    machine_id: str
    mac_machine_id: str
    client_version: str

    def __repr__(self) -> str:
        return "_SessionMaterial(redacted)"


def default_state_dir() -> Path:
    """macOS Cursor globalStorage. Live calls only; tests inject a directory."""

    return Path.home() / "Library/Application Support/Cursor/User/globalStorage"


def default_version_path() -> Path:
    return Path("/Applications/Cursor.app/Contents/Resources/app/package.json")


def load_session(
    *,
    state_dir: Path | None = None,
    version_path: Path | None = None,
    client_version: str | None = None,
) -> _SessionMaterial | ProviderCollectionFailure:
    """Read login material. This function does not write Cursor files."""

    root = default_state_dir() if state_dir is None else state_dir
    token = _read_token(root / "state.vscdb")
    if isinstance(token, ProviderCollectionFailure):
        return token
    machine_ids = _read_machine_ids(root / "storage.json")
    if isinstance(machine_ids, ProviderCollectionFailure):
        return machine_ids
    machine_id, mac_machine_id = machine_ids
    version = client_version
    if version is None:
        resolved_version = _read_client_version(
            default_version_path() if version_path is None else version_path
        )
        if isinstance(resolved_version, ProviderCollectionFailure):
            return resolved_version
        version = resolved_version
    elif not _usable_header_value(version, max_length=40):
        return _failure("missing_client_version", "not_installed")
    return _SessionMaterial(
        token=token,
        machine_id=machine_id,
        mac_machine_id=mac_machine_id,
        client_version=version,
    )


def build_request(material: _SessionMaterial) -> PreparedRequest:
    """SECURITY: this is the only builder that may receive the access token."""

    # PROVIDER: feasibility record uses this unofficial checksum shape.
    # Keep it here so a header change does not spread into the parser（PR #6）。
    checksum = f"00000000{material.machine_id}/{material.mac_machine_id}"
    headers = (
        ("Content-Type", "application/json"),
        ("Connect-Protocol-Version", "1"),
        ("Authorization", f"Bearer {material.token}"),
        ("x-cursor-client-type", "ide"),
        ("x-cursor-client-version", material.client_version),
        ("x-cursor-checksum", checksum),
    )
    return PreparedRequest(url=_PERIOD_USAGE_URL, body=b"{}", headers=headers)


def urllib_transport(
    request: PreparedRequest, *, deadline_seconds: float
) -> TransportResponse | ProviderCollectionFailure:
    """POST with a bounded deadline. Redirects are not followed."""

    if deadline_seconds <= 0:
        return _failure("deadline_exceeded", "timeout")
    opener = urllib.request.build_opener(_RejectRedirects())
    outgoing = urllib.request.Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method="POST",
    )
    try:
        with opener.open(outgoing, timeout=deadline_seconds) as response:
            status = int(response.status)
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except TimeoutError:
        return _failure("deadline_exceeded", "timeout")
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        mapped = map_http_status(status)
        if mapped is not None:
            return mapped
        return _failure("upstream_http_error", "upstream")
    except (urllib.error.URLError, OSError):
        return _failure("connection_failed", "network")
    if len(body) > _MAX_RESPONSE_BYTES:
        return _failure("response_too_large", "malformed_response")
    mapped = map_http_status(status)
    if mapped is not None:
        return mapped
    return TransportResponse(status=status, body=body)


def map_http_status(status: int) -> ProviderCollectionFailure | None:
    """Map a non-200 HTTP status. 200 returns None so the body can be parsed.

    401 is `auth_expired`. Production urllib and injected transports both use
    this function so a 4xx response cannot be marked retryable by only one path.
    """

    if status == 200:
        return None
    if status == 401:
        return _failure("token_rejected", "auth_expired")
    # PROVIDER: 只有 5xx 值得 retry。403 這類 4xx 重送不會變成功（PR #6）。
    return ProviderCollectionFailure(
        provider_id=CURSOR_PROVIDER_ID,
        source=CURSOR_SOURCE,
        category="upstream",
        message=_FAILURE_MESSAGES["upstream_http_error"],
        retryable=status >= 500,
        code="upstream_http_error",
    )


def decode_period_usage_body(
    body: bytes,
) -> object | ProviderCollectionFailure:
    """Decode a response body without copying it into the failure message."""

    if len(body) > _MAX_RESPONSE_BYTES:
        return _failure("response_too_large", "malformed_response")
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        return _failure("malformed_json", "malformed_response")
    payload = _decode_json_text(text)
    if payload is None:
        return _failure("malformed_json", "malformed_response")
    return payload


def _decode_json_text(text: str) -> object | None:
    """Parse one JSON value. Invalid, nonstandard, or unsafe input returns None."""

    stripped = text.strip()
    if not stripped:
        return None
    decoder = json.JSONDecoder(parse_constant=_reject_nonstandard_constant)
    try:
        parsed, end = decoder.raw_decode(stripped)
    except (json.JSONDecodeError, ValueError, RecursionError):
        # CONTRACT: 超大整數是 ValueError，過深巢狀是 RecursionError。
        # NaN／Infinity 不是 RFC 8259，不能進 success（PR #6）。
        return None
    if stripped[end:].strip():
        return None
    value: object = parsed
    return value


def _reject_nonstandard_constant(_literal: str) -> NoReturn:
    raise ValueError("non-standard JSON constant")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Drop redirects so the Authorization header is not sent to another host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _read_token(db_path: Path) -> str | ProviderCollectionFailure:
    if not db_path.is_file():
        return _failure("missing_local_state", "not_installed")
    quoted = quote(db_path.resolve().as_posix(), safe="/:")
    uri = f"file:{quoted}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return _failure("unreadable_local_state", "not_installed")
    try:
        try:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (_TOKEN_KEY,),
            ).fetchone()
        except sqlite3.Error:
            return _failure("unreadable_local_state", "not_installed")
    finally:
        connection.close()
    if row is None:
        return _failure("missing_login", "not_authenticated")
    token = _decode_token(row[0])
    if token is None:
        return _failure("missing_login", "not_authenticated")
    return token


def _read_machine_ids(storage_path: Path) -> tuple[str, str] | ProviderCollectionFailure:
    if not storage_path.is_file():
        return _failure("missing_local_state", "not_installed")
    try:
        text = storage_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return _failure("unreadable_local_state", "not_installed")
    payload = _decode_json_text(text)
    if not isinstance(payload, dict):
        return _failure("unreadable_local_state", "not_installed")
    machine_id = _header_value(payload, _MACHINE_ID_KEY)
    mac_machine_id = _header_value(payload, _MAC_MACHINE_ID_KEY)
    if machine_id is None or mac_machine_id is None:
        return _failure("missing_local_state", "not_installed")
    return machine_id, mac_machine_id


def _read_client_version(path: Path) -> str | ProviderCollectionFailure:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return _failure("missing_client_version", "not_installed")
    payload = _decode_json_text(text)
    if not isinstance(payload, dict):
        return _failure("missing_client_version", "not_installed")
    version = payload.get("version")
    if not isinstance(version, str) or not _usable_header_value(version, max_length=40):
        return _failure("missing_client_version", "not_installed")
    return version


def _header_value(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str) or not _usable_header_value(value, max_length=256):
        return None
    return value


def _decode_token(value: object) -> str | None:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeError:
            return None
    elif isinstance(value, str):
        text = value
    else:
        return None
    text = text.strip()
    if text.startswith('"'):
        decoded = _decode_json_text(text)
        if not isinstance(decoded, str):
            return None
        text = decoded
    if not _usable_header_value(text, max_length=8192):
        return None
    return text


def _usable_header_value(value: str, *, max_length: int) -> bool:
    return 1 <= len(value) <= max_length and "\n" not in value and "\r" not in value


def _failure(code: str, category: ErrorCategory) -> ProviderCollectionFailure:
    return ProviderCollectionFailure(
        provider_id=CURSOR_PROVIDER_ID,
        source=CURSOR_SOURCE,
        category=category,
        message=_FAILURE_MESSAGES[code],
        retryable=_RETRYABLE.get(code, False),
        code=code,
    )
