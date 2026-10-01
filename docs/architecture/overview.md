# Architecture Overview

## System boundary

Agent Meter 由 Local Collector 與 ESP32 Display Client 組成。Collector 是唯一能接觸 provider login state 與 upstream API 的 component；ESP32 只讀取 normalized local API。Desktop Web MVP 與 ESP32 共用同一份 usage contract；本機 UI 另外讀寫 settings。

```text
Cursor ─┐
Codex ──┼─> Provider adapters ─> Normalizer ─> Orchestrator ─> Snapshot cache ─> HTTP API
Claude ─┘                         ^                ^                │              │
                                  │                │                │              ├─> Desktop UI
                         settings store      enablement filter     │              └─> ESP32
```

One-shot CLI (`python -m agent_meter`) 仍一次收集三個 adapter，不讀 desktop settings。常駐 service 只收集 `enabled_providers`。

## Dependency direction

```text
provider transport -> adapter parser -> domain model <- API/cache/ESP32/settings contract
                                      ^
                                      └─ orchestrator coordinates only
```

- Domain model 不 import provider-specific modules。
- Provider adapters 可以依賴 domain model，但不能依賴 API 或 ESP32 code。
- Settings、health、error envelope 不依賴 FastAPI。
- API 與 cache 只接受通過 schema validation 的 normalized snapshot。
- ESP32 只依賴 versioned usage contract，不依賴 provider 名稱以外的 upstream 細節。

## Ownership

- **Adapter**：provider discovery、read-only auth access、transport、raw validation、欄位／單位轉換。
- **Normalizer/domain**：共用 types、range validation、status 與 meter semantics。
- **Domain policy**：top-level aggregation、freshness/stale threshold、last-good merge；純函式，沒有 I/O。見 `src/agent_meter/freshness.py`、`aggregation.py`、`cache.py`。
- **Settings**：typed enablement document 與 PUT body。見 `src/agent_meter/settings.py`。
- **Runtime paths**：checkout-external config／cache／mailbox 路徑。見 `src/agent_meter/paths.py`。
- **Settings store**：bounded read、0700／0600、symlink 拒絕、atomic CAS。見 `src/agent_meter/settings_store.py`。
- **Service DTOs／policy**：health、error envelope、三種 503。見 `src/agent_meter/service_models.py`、`service_policy.py`。
- **Orchestrator**：獨立呼叫 adapters、timeout／throw isolation、呼叫 domain policy、寫出 gitignored `usage.json`。常駐 scheduler 與 HTTP 尚未建立。
- **Cache**：in-memory last-good merge 在 `cache.py`；disk envelope 與 restart restore 在 `cache_store.py`。不保存 credential 或 raw response。
- **HTTP API**：提供 schema-valid snapshot、health 與 settings；不主動 refresh provider。尚未建立。
- **ESP32**：poll、bounded parse、render、offline recovery。

## Agent navigation

1. `/AGENTS.md`
2. `/docs/contracts/README.md`
3. `/schemas/usage-v0.1.schema.json`、`/schemas/settings-v1.schema.json`、`/schemas/health-v1.schema.json`、`/schemas/error-v1.schema.json`、`/schemas/cache-v1.schema.json`
4. `/tests/fixtures/contracts/`、`/tests/fixtures/settings/`、`/tests/fixtures/service/`
5. 對應的 `/docs/providers/<provider>.md`
6. Collector package root：`/src/agent_meter/`
7. Offline tests：`/tests/`
8. Local/CI quality entrypoint：`/scripts/quality.sh`
9. Foundation toolchain decision：`/docs/adr/0001-collector-python-foundation.md`
10. Desktop settings／DTO decision：`/docs/adr/0002-desktop-settings-and-service-dtos.md`

Normalized usage models 在 `src/agent_meter/models.py`。Aggregation、stale 與 cache envelope 在 `freshness.py`、`aggregation.py`、`cache.py`。Desktop settings 在 `settings.py`、`paths.py`、`settings_store.py`。Disk cache 在 `cache_store.py` 與 `private_files.py`。Health／error DTO 與 503 決策在 `service_models.py`、`service_policy.py`。Claude Code status-line adapter 在 `src/agent_meter/providers/claude.py`。Codex app-server adapter 在 `src/agent_meter/providers/codex.py` 與 `codex_rpc.py`。Cursor period-usage parser 在 `src/agent_meter/providers/cursor.py`，read-only Connect RPC client 在 `cursor_rpc.py`。One-shot orchestrator 在 `src/agent_meter/orchestrator.py`（`python -m agent_meter`）。HTTP API 尚未建立。各 component 的入口 module 必須連回相關 contract，並以 `CONTRACT:`、`SECURITY:`、`PROVIDER:`、`FALLBACK:` 標記不容易從 type system 看出的關鍵 invariant。
