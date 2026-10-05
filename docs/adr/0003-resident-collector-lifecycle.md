# ADR 0003: Resident collector uses per-provider generation and dual clocks

- Status: Accepted for Batch 2 desktop service
- Date: 2026-10-02
- Task: [B2-03 Collector Provider lifecycle](https://app.notion.com/p/3eadf30cc71c81bb91dacaea1a243913)

## Context

One-shot `python -m agent_meter` collects every adapter once. The desktop service must stay up, honor `enabled_providers`, and isolate a disable/re-enable race without blocking every provider behind one worker. HTTP still belongs to a later change.

## Decision

- Own lifecycle in `CollectorService`, not in the one-shot orchestrator.
- Tag every collection with a per-provider generation. Disable and re-enable both bump it; late results are dropped.
- Schedule Codex/Cursor with an injected monotonic clock; stamp snapshots with an injected wall clock.
- Bound concurrency to one in-flight job per polled provider (max two threads).
- Keep Claude as an event-source stub: `not_configured` until a typed event arrives. Do not poll the Claude API.
- Exclusive-lock the runtime config directory so a second instance cannot share writers.

## Consequences

GET /usage remains a read of the projected snapshot. B2-04 FastAPI handlers inject this service. B2-05 supplies Claude mailbox events through `ingest_claude_event` with the current generation. Shutdown cannot kill Python threads; collect and persist workers are daemon so the process can exit after the deadline, adapters must honor `deadline_seconds`, and generation fencing still discards late writes. Cache persist is coalesced on a dedicated writer so a stalled fsync cannot occupy both provider slots.
