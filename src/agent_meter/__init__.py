"""Agent Meter Collector package root.

This module is the installable package marker for the local Collector.
It does not collect provider usage, expose HTTP, or validate snapshots.

Inputs: none. This module has no runtime I/O.
Outputs: package metadata used by quality checks and later modules.

The usage snapshot contract remains `docs/contracts/` and
`schemas/usage-v0.1.schema.json`. Retry, cache, and fallback belong to later
Collector service modules, not this foundation package.
"""

__version__ = "0.1.0"
