"""Agent Meter Collector package root.

Collector 可安裝 package 標記（PR #1）。Normalized snapshot models live in
`agent_meter.models`（PR #2）。One-shot orchestration lives in
`agent_meter.orchestrator`（PR #7）. Desktop settings and service DTOs live
in `agent_meter.settings`, `paths`, `settings_store`, `cache_store`, `service_models`, and
`service_policy`. This package still does not serve HTTP.

Inputs: none. This module has no runtime I/O.
Outputs: package metadata used by quality checks and later modules.

The usage snapshot contract remains `docs/contracts/usage-api.md` and
`schemas/usage-v0.1.schema.json`. Settings and desktop HTTP documents are
`docs/contracts/settings.md` and `docs/contracts/desktop-service.md`.
Persistent cache restore lives in `cache_store.py`. GET /usage HTTP still
belongs to a later Collector service module.
"""

__version__ = "0.1.0"
