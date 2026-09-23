"""Serialize typed collection results for machine-readable stdout.

Responsibility: dump success/failure without copying unknown upstream fields.
Non-goals: provider transport, parsing, or cache I/O.
"""

from __future__ import annotations

from agent_meter.cache import ProviderCollectionFailure, ProviderCollectionSuccess


def dump_collection_result(
    result: ProviderCollectionSuccess | ProviderCollectionFailure,
) -> dict[str, object]:
    """Serialize a typed result without copying unknown upstream fields."""

    # SECURITY: dump only normalized fields. Provider extras such as account
    # identifiers and raw RPC envelopes must never appear here.
    if isinstance(result, ProviderCollectionSuccess):
        return {
            "result": "success",
            "provider_id": result.provider_id,
            "source": result.source,
            "collected_at": result.collected_at,
            "meters": [
                meter.model_dump(mode="json", exclude_unset=True) for meter in result.meters
            ],
        }
    payload: dict[str, object] = {
        "result": "failure",
        "provider_id": result.provider_id,
        "source": result.source,
        "category": result.category,
        "message": result.message,
        "retryable": result.retryable,
    }
    if result.code is not None:
        payload["code"] = result.code
    return payload
