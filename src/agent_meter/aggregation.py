"""Top-level usage status aggregation.

Responsibility: fold per-provider statuses into snapshot `ok` / `partial` /
`error`, after freshness has been recomputed. Non-goals: provider I/O,
persistent cache, or HTTP.

Inputs: provider id → status or snapshot map; `now` as UTC Unix seconds.
Outputs: top-level status, or a rebuilt `UsageSnapshot` with `generated_at=now`.

Contract: main plan + `docs/contracts/usage-api.md`. Single-provider failure
does not change other providers' snapshots; this module only reads the map it
is given.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from agent_meter.freshness import evaluate_provider
from agent_meter.models import ProviderSnapshot, UsageSnapshot

ProviderStatus = Literal["ok", "stale", "unavailable", "error"]
TopLevelStatus = Literal["ok", "partial", "error"]

_DISPLAYABLE: frozenset[ProviderStatus] = frozenset({"ok", "stale"})


def aggregate_status(statuses: Mapping[str, ProviderStatus]) -> TopLevelStatus:
    """Aggregate configured provider statuses into a top-level snapshot status."""

    # CONTRACT: empty configured set has no displayable data → error, never a
    # fabricated ok snapshot.
    if not statuses:
        return "error"
    values = list(statuses.values())
    if not any(status in _DISPLAYABLE for status in values):
        return "error"
    # CONTRACT: ok only when every configured provider is still ok (fresh).
    # All-stale is displayable last-good data, so partial rather than error.
    if all(status == "ok" for status in values):
        return "ok"
    return "partial"


def build_snapshot(
    providers: Mapping[str, ProviderSnapshot],
    *,
    now: int,
) -> UsageSnapshot:
    """Evaluate freshness then aggregate. `providers` must be non-empty."""

    if not providers:
        raise ValueError("providers must not be empty")
    evaluated = {
        provider_id: evaluate_provider(snapshot, now=now)
        for provider_id, snapshot in providers.items()
    }
    return UsageSnapshot(
        schema_version="0.1",
        generated_at=now,
        status=aggregate_status(
            {provider_id: snapshot.status for provider_id, snapshot in evaluated.items()}
        ),
        providers=dict(evaluated),
    )
