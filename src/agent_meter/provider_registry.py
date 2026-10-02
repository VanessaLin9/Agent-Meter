"""Lazy provider collector factory for the resident service（B2-03）.

Responsibility: hand out per-provider collect callables without reading
login state, spawning processes, or sending network requests at factory
construction or `collector_for` time. Non-goals: scheduling, cache merge,
Claude mailbox I/O, or new authentication.

Inputs: an enabled provider id at dispatch time. Outputs: a
`ProviderCollector` that may touch live state only when invoked.
Claude is event-driven and is not produced here.

Contract: `docs/contracts/collector-service.md` and
`docs/contracts/provider-adapter.md`.
"""

from __future__ import annotations

from typing import TextIO

from agent_meter.cache import ProviderCollectionFailure, ProviderCollectionSuccess
from agent_meter.orchestrator import ProviderCollector
from agent_meter.settings import EnabledProvider


class ProviderCollectorFactory:
    """Tests inject spies; production uses LazyLiveCollectorFactory."""

    def collector_for(self, provider_id: EnabledProvider) -> ProviderCollector:
        raise NotImplementedError


class LazyLiveCollectorFactory(ProviderCollectorFactory):
    """Bind Codex/Cursor adapters only inside the collect callable.

    SECURITY: importing this module, constructing the factory, or calling
    collector_for must not read Cursor session files or spawn Codex.
    Disabled providers never call collector_for.
    """

    def __init__(self, *, stderr: TextIO | None = None) -> None:
        self._stderr = stderr

    def collector_for(self, provider_id: EnabledProvider) -> ProviderCollector:
        if provider_id == "codex":
            stderr = self._stderr

            def collect_codex(
                *, now: int, deadline_seconds: float
            ) -> ProviderCollectionSuccess | ProviderCollectionFailure:
                # PROVIDER: live app-server spawn stays inside this call.
                from agent_meter.providers.codex import collect as collect_codex_live

                return collect_codex_live(now=now, deadline_seconds=deadline_seconds, stderr=stderr)

            return collect_codex
        if provider_id == "cursor":

            def collect_cursor(
                *, now: int, deadline_seconds: float
            ) -> ProviderCollectionSuccess | ProviderCollectionFailure:
                # PROVIDER: session read and Connect RPC stay inside this call.
                from agent_meter.providers.cursor import collect as collect_cursor_live

                return collect_cursor_live(now=now, deadline_seconds=deadline_seconds)

            return collect_cursor
        raise LookupError("claude is event-driven and has no poll collector")
