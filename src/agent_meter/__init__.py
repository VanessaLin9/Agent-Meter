"""Agent Meter Collector package root.

Collector 可安裝 package 標記（PR #1）。Normalized snapshot models live in
`agent_meter.models`（PR #2）。One-shot orchestration lives in
`agent_meter.orchestrator`. This package still does not serve HTTP.

Inputs: none. This module has no runtime I/O.
Outputs: package metadata used by quality checks and later modules.

The usage snapshot contract remains `docs/contracts/` and
`schemas/usage-v0.1.schema.json`. Scheduler, persistent cache restore, and
GET /usage belong to later Collector service modules.
"""

__version__ = "0.1.0"
