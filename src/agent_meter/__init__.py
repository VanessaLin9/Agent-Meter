"""Agent Meter Collector package root.

Collector 可安裝 package 標記（PR #1）。Normalized snapshot models live in
`agent_meter.models`（PR #2）。This package still does not collect provider
usage or serve HTTP.

Inputs: none. This module has no runtime I/O.
Outputs: package metadata used by quality checks and later modules.

The usage snapshot contract remains `docs/contracts/` and
`schemas/usage-v0.1.schema.json`. Retry, cache, and fallback belong to later
Collector service modules, not this package root.
"""

__version__ = "0.1.0"
