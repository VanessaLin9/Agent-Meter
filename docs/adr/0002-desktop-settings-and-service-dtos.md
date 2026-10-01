# ADR 0002: Desktop settings live outside the checkout with separate service DTOs

- Status: Accepted for Batch 2 desktop service
- Date: 2026-10-01
- Task: [B2-01 Provider 啟用設定與 Desktop service 契約](https://app.notion.com/p/3eadf30cc71c8192b0faeb12ed3eac46)

## Context

Batch 1 delivered a one-shot collector that writes gitignored `usage.json`. The desktop service needs a user-owned enablement list, checkout-external runtime files, and HTTP documents that are not usage snapshots. Mixing those into `usage-v0.1` would either allow an empty `providers` map or invent a `disabled` status.

## Decision

- Persist a v1 settings document (`settings_version`, `revision`, `enabled_providers`) under `~/Library/Application Support/Agent Meter`.
- Keep normalized cache and the Claude mailbox under `~/Library/Caches/Agent Meter`.
- Own health and error envelopes in dedicated schemas. GET /usage 200 remains usage v0.1; three 503 codes use the error envelope.
- Default desktop enablement is empty. One-shot `--live` stays a separate entrypoint and still collects every integrated adapter.
- Domain modules own the DTOs. FastAPI is not a dependency of settings, health, or enablement policy.

## Consequences

Downstream scheduler, loopback API, and UI share one CAS revision and one enabled list. Empty enablement is a 503, not a schema change. Corrupt settings fail closed instead of resetting to all providers.
