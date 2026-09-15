# Architecture Overview

## System boundary

Agent Meter 由 Local Collector 與 ESP32 Display Client 組成。Collector 是唯一能接觸 provider login state 與 upstream API 的 component；ESP32 只讀取 normalized local API。

```text
Cursor ─┐
Codex ──┼─> Provider adapters ─> Normalizer ─> Orchestrator ─> Snapshot cache ─> HTTP API
Claude ─┘                                                                  │
                                                                           └─> ESP32
```

## Dependency direction

```text
provider transport -> adapter parser -> domain model <- API/cache/ESP32 contract
                                      ^
                                      └─ orchestrator coordinates only
```

- Domain model 不 import provider-specific modules。
- Provider adapters 可以依賴 domain model，但不能依賴 API 或 ESP32 code。
- API 與 cache 只接受通過 schema validation 的 normalized snapshot。
- ESP32 只依賴 versioned usage contract，不依賴 provider 名稱以外的 upstream 細節。

## Ownership

- **Adapter**：provider discovery、read-only auth access、transport、raw validation、欄位／單位轉換。
- **Normalizer/domain**：共用 types、range validation、status 與 meter semantics。
- **Orchestrator**：refresh scheduling、timeout、typed failure mapping、last-good fallback、aggregation。
- **Cache**：atomic persistence 與 restart recovery；不保存 credential 或 raw response。
- **HTTP API**：提供 schema-valid snapshot 與 health；不主動 refresh provider。
- **ESP32**：poll、bounded parse、render、offline recovery。

## Agent navigation

1. `/AGENTS.md`
2. `/docs/contracts/README.md`
3. `/schemas/usage-v0.1.schema.json`
4. `/tests/fixtures/contracts/`
5. 對應的 `/docs/providers/<provider>.md`
6. Collector package root：`/src/agent_meter/`
7. Offline tests：`/tests/`
8. Local/CI quality entrypoint：`/scripts/quality.sh`
9. Foundation toolchain decision：`/docs/adr/0001-collector-python-foundation.md`

Milestone 0 只有 package 骨架與品質工具。Adapter、normalizer、orchestrator、cache 與 HTTP API 尚未建立。各 component 的入口 module 必須連回相關 contract，並以 `CONTRACT:`、`SECURITY:`、`PROVIDER:`、`FALLBACK:` 標記不容易從 type system 看出的關鍵 invariant。
