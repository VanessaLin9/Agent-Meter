# Settings Persistence Contract

Machine-readable sources:

- [`../../schemas/settings-v1.schema.json`](../../schemas/settings-v1.schema.json)
- [`../../schemas/settings-write-v1.schema.json`](../../schemas/settings-write-v1.schema.json)

HTTP mapping for these documents lives in [`desktop-service.md`](desktop-service.md). This file owns the on-disk document, path layout, CAS, and permission boundary.

## Document

Persisted `settings.json` and `GET /settings` share one document:

- `settings_version`：固定為 `1`。未知版本是 `config_error`，不得當首次安裝、不得覆寫。
- `revision`：非負整數 CAS token。缺檔首次安裝為 `0`。每次成功 save 加 1。
- `enabled_providers`：`codex` / `cursor` / `claude` 的不重複清單，順序保留。空陣列合法。

未知欄位、未知 ID、重複值、錯誤型別一律拒絕。v1 沒有 secret 欄位，也不得新增 token、session、帳號或任意檔案路徑欄位。

`PUT /settings` body 只有 `expected_revision` 與 `enabled_providers`。成功後回傳新的 settings document（含新 `revision`）。

## Runtime paths

路徑由 resolver 組成，測試注入 temp dir，不得寫死使用者 home，也不得把 runtime 放進 Git checkout。

macOS 預設：

| 用途 | 目錄 |
| --- | --- |
| settings | `~/Library/Application Support/Agent Meter/settings.json` |
| normalized snapshot cache | `~/Library/Caches/Agent Meter/snapshots/` |
| Claude normalized mailbox | `~/Library/Caches/Agent Meter/mailbox/` |

本張只實作 settings 檔的讀寫。cache 與 mailbox 目錄由後續任務建立。

## Filesystem rules

- 缺檔（且不是 dangling symlink）視為首次安裝：`enabled_providers=[]`、`revision=0`，不自動建檔。
- 目錄 `0700`、檔案 `0600`。既有 settings 檔若權限過寬、不是普通檔、或為 symlink，視為 `config_error`。
- 讀取 bounded（16 KiB）。更大、非 UTF-8、非 JSON object、驗證失敗都是 `config_error`。
- 損壞檔、未知版本、讀取拒絕：不啟用任何 provider、不自動覆寫原檔。
- 寫入必須 atomic replace。replace 失敗回 `save_failed`，保留舊檔，不把半份 JSON 留給 reader。
- `expected_revision` 不符回 `revision_conflict`，不寫檔。
- Store 每次 `load()` 都讀當前 bytes。Desktop service 不得 watch 檔案；外部手改需重啟才進 runtime。只有 UI／`PUT /settings` 這條 CAS 路徑套用新設定。

## Error codes (domain)

| code | 何時 |
| --- | --- |
| `config_error` | 檔案損壞、未知版本、權限／symlink、過大、無法讀取、或 payload 驗證失敗 |
| `revision_conflict` | CAS mismatch |
| `save_failed` | atomic replace 或建目錄失敗 |

公開錯誤訊息不得包含檔案內容、planted secret 或本機路徑。HTTP status 由 API 層決定，本 module 不 import FastAPI。

## Ownership

- Typed settings：`src/agent_meter/settings.py`
- Path resolver：`src/agent_meter/paths.py`
- Disk CAS：`src/agent_meter/settings_store.py`
- Retry／runtime apply／generation fencing：尚未實作的 desktop service（B2-03）
