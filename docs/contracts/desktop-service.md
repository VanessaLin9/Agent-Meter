# Desktop Service Contract

Machine-readable sources:

- [`../../schemas/usage-v0.1.schema.json`](../../schemas/usage-v0.1.schema.json)
- [`../../schemas/settings-v1.schema.json`](../../schemas/settings-v1.schema.json)
- [`../../schemas/settings-write-v1.schema.json`](../../schemas/settings-write-v1.schema.json)
- [`../../schemas/health-v1.schema.json`](../../schemas/health-v1.schema.json)
- [`../../schemas/error-v1.schema.json`](../../schemas/error-v1.schema.json)
- [`../../schemas/cache-v1.schema.json`](../../schemas/cache-v1.schema.json)

本檔定義桌面 Collector service 的跨層 DTO、啟用語意與失敗碼。FastAPI handlers 尚未實作；API 層之後只能接線，不得改這些欄位。

## Shared DTO owner

| Document | Owner | Consumers |
| --- | --- | --- |
| Usage snapshot v0.1 | `src/agent_meter/models.py` | GET /usage 200、cache、ESP32 |
| Settings v1 | `src/agent_meter/settings.py` | GET /settings、disk、CAS |
| Settings write v1 | `src/agent_meter/settings.py` | PUT /settings body |
| Health v1 | `src/agent_meter/service_models.py` | GET /health |
| Error envelope v1 | `src/agent_meter/service_models.py` | 非 snapshot 的 HTTP error |
| Enablement / 503 決策 | `src/agent_meter/service_policy.py` | 未來 API 與 scheduler |
| Disk cache v1 | `src/agent_meter/cache_store.py` | 重啟恢復；不得當 GET /usage body |

HTTP error envelope **不是** usage snapshot。不得把它送進 `parse_usage_snapshot`。usage v0.1 schema 維持 `providers` 至少一個 key；空清單不得靠新增 `disabled` status 或空 `providers` 混進 200。

## Settings commit order

1. 驗證 PUT body（未知欄位／ID／重複／型別 → 拒絕，不寫檔）。
2. 讀目前 disk document，比對 `expected_revision`。
3. Atomic persist 新 document（`revision + 1`）。
4. 成功後才允許 runtime 套用新的 enabled 清單並遞增 collection generation。
5. persist 失敗：舊設定與舊 runtime 都不動；UI 不得顯示假成功。

外部手改 `settings.json` 不做 hot reload。重啟後從最後一次成功保存的版本恢復。

## Enablement and disable races

同一個 `enabled_providers` 清單同時控制收集與顯示。沒有「隱藏但仍收集」。

- 只有 enabled ID 能進入 snapshot／aggregate。
- 停用後：零新 I/O（不排程、不讀 session、不發 request、不建子程序）。
- 已送出的工作無法撤回；必須 bounded cancellation，並以 generation fencing 丟棄晚到結果。
- off→on 不得接續停用前那筆 in-flight 工作。
- last-good 可留在內部 cache，但 disabled 期間不得輸出、也不得刷新年齡。`collected_at` 仍是資料年齡。
- 啟用但未安裝／未登入仍是 `unavailable`，不得自動取消勾選。

One-shot CLI `python -m agent_meter --live` 仍收集全部已整合 adapter；空的 desktop `enabled_providers` 不得回寫去改 CLI 預設。

## Endpoints

本批只 bind `127.0.0.1`。UI 與 API 同源；settings write 是這個同源 UI 的例外寫入能力，不是 LAN 開放。Host／CORS／CSRF 細節由 B2-04 實作，但不得放寬成 wildcard CORS 或非 loopback bind。

### GET /settings

- 首次安裝或缺檔：200，空清單、`revision=0`。
- 損壞／未知版本／權限拒絕：error envelope，`error.code=config_error`。不得改回傳成空清單。

### PUT /settings

Body：`expected_revision` + `enabled_providers`。成功 回新 settings document。CAS mismatch：`revision_conflict`。save 失敗：`save_failed`。

### GET /health

一律表示 process 活著，不能只憑 HTTP 200 宣稱 provider 健康。body `state`：

- `idle`：設定可讀且沒有任何 enabled provider。
- `ready`：設定可讀且至少啟用一個 provider（snapshot 是否就緒不在這裡表示）。
- `degraded`：設定無法使用（`config_error`）或 cache 無法使用（`cache_error`）。

只含白名單 metadata：`state` 與 optional `error`。

### GET /usage

成功產生 schema-valid snapshot 時仍是 200，即使 top-level `status` 是 `partial` 或 `error`。下列情況不得回 snapshot，改回 503 與 error envelope：

| `error.code` | 何時 |
| --- | --- |
| `no_enabled_providers` | 設定可讀且 `enabled_providers=[]` |
| `snapshot_not_ready` | 已啟用，但還沒有涵蓋整個 active set 的 snapshot |
| `snapshot_unavailable` | 設定損壞，或內部無法產生任何有效 snapshot |

`config_error` 不得偽裝成 `no_enabled_providers`。

## Polling (scheduler not in this change)

- Codex／Cursor：每 300 秒、timeout 20 秒。同 provider single-flight，不堆積錯過的 tick。
- 失敗後下次 retry 退避上限 900 秒；一次排程內不無限 retry。
- `stale_after_seconds` 預設 900，沿用既有 freshness 邊界。
- Claude 是 event-driven mailbox，不套用這個 poller。

## Fallback summary

| 情況 | GET /settings | PUT /settings | GET /health | GET /usage |
| --- | --- | --- | --- | --- |
| 首次安裝／全部停用 | 200 空清單 | CAS 成功可寫入 | `idle` | 503 `no_enabled_providers` |
| 已啟用、snapshot 未齊 | 200 | CAS | `ready` | 503 `snapshot_not_ready` |
| 內部無法產生 snapshot | 200 | CAS | `ready` | 503 `snapshot_unavailable` |
| 設定損壞 | 503/error `config_error` | 拒絕寫入 | `degraded` + `config_error` | 503 `snapshot_unavailable` |
| cache 損壞或 save 失敗 | 200 | CAS | `degraded` + `cache_error` | memory last-good 或 `snapshot_not_ready` |
| CAS mismatch | — | `revision_conflict`，不寫檔 | 不變 | 不變 |
| atomic save 失敗 | 舊 document | `save_failed`，runtime 不變 | 不變 | 不變 |
