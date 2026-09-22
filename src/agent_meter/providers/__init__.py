"""Provider adapters.

Responsibility: provider-specific discovery, transport, raw validation, and
normalization input. Non-goals: HTTP API, cache I/O, scheduling, or
aggregation. Retry and last-good fallback belong to the orchestrator.
"""
