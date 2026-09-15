"""Agent Meter Collector package root.

Collector 可安裝 package 標記（PR #1）。不收集 provider usage、不提供 HTTP、不驗證 snapshot。

Inputs: none. This module has no runtime I/O.
Outputs: package metadata used by quality checks and later modules.

The usage snapshot contract remains `docs/contracts/` and
`schemas/usage-v0.1.schema.json`. Retry, cache, and fallback belong to later
Collector service modules, not this foundation package.
"""

__version__ = "0.1.0"
