# Usage API Contract v0.1

Machine-readable source: [`../../schemas/usage-v0.1.schema.json`](../../schemas/usage-v0.1.schema.json)

## Endpoint

`GET /usage` 回傳 Collector 最新的 normalized snapshot。

- 成功產生 schema-valid snapshot 時回傳 `200 OK`，即使 top-level `status` 是 `partial` 或 `error`。
- 下列情況回傳 `503 Service Unavailable` 與 error envelope（見 [`desktop-service.md`](desktop-service.md)），**不得**把該 body 當 usage snapshot 解析：
  - `no_enabled_providers`：desktop settings 可讀且沒有任何 enabled provider。
  - `snapshot_not_ready`：已啟用，但還沒有涵蓋整個 active set 的 snapshot。
  - `snapshot_unavailable`：設定損壞，或內部無法產生任何 schema-valid snapshot。
- Endpoint 本身不觸發同步 provider refresh，只讀取目前 memory/cache snapshot。
- Response 不得包含 raw upstream response、credential、local account identifier 或 debug stack trace。
- 本批 Local API 只 bind loopback；見 [`security-and-testing.md`](security-and-testing.md)。

## Top-level fields

- `schema_version`：目前固定為 `0.1`。
- `generated_at`：本 snapshot 產生時間，UTC Unix seconds。
- `status`：`ok`、`partial`、`error`。
- `providers`：以穩定 provider ID 為 key 的 snapshot map。

Top-level aggregation：

- `ok`：所有已設定 provider 都是 `ok`。
- `partial`：至少一個 provider 有可顯示資料，另有 provider 是 `stale`、`unavailable` 或 `error`。
- `error`：沒有任何 provider 有可顯示資料。

Desktop service 的「已設定」等於 `enabled_providers`。只有 enabled ID 出現在 `providers` map 並參與 top-level aggregation。停用的 provider 不得輸出 last-good，也不得刷新年齡。v0.1 schema 仍要求 `providers` 至少一個 key，所以空 enablement 走 503，不新增 `disabled` status、也不回空 map。

One-shot CLI 仍以三個已整合 adapter 為 configured set；它不讀 desktop `settings.json`。

## Provider snapshot

- `status`：`ok`、`stale`、`unavailable`、`error`。
- `source`：不含敏感資訊的穩定 adapter source ID。
- `collected_at`：最後一次成功取得資料的 UTC Unix seconds；從未成功時為 `null`。
- `stale_after_seconds`：本 provider 資料在多久後視為 stale。
- `meters`：目前可顯示 meters。沒有有效資料時必須是空陣列。
- `error`：sanitized failure summary；`ok` 時不得存在。

Provider status semantics：

- `ok`：本次 refresh 成功，至少有一個 validated meter。
- `stale`：本次 refresh 失敗，但保留至少一個 last-good meter。
- `unavailable`：provider 未安裝、未設定或沒有可用 login state；沒有 meter。
- `error`：refresh 失敗且沒有 last-good meter；沒有 meter。

## Meter

共用欄位：

- `id`：provider 內穩定、machine-readable 的 snake_case ID。
- `label`：短顯示名稱；ESP32 可以使用，但不能拿來作邏輯判斷。
- `kind`：`quota` 或 `spend`。
- `unit`：`percent`、`currency`、`requests`、`tokens`。
- `reset_at`：可選；UTC Unix seconds。無 reset 資訊時為 `null` 或省略。

Quota meter：

- 必須有 `remaining_percentage`，範圍為 0 到 100。
- `used`、`limit`、`remaining` 可以存在，但都是 optional，不能假設 upstream 一定提供。

Spend meter：

- 必須有 `used` 與 ISO 4217 `currency_code`。
- `limit`、`remaining`、`remaining_percentage` 可以存在。
- Provider 沒有回傳 spend value 時，省略整個 spend meter，不建立全為 `null` 的 meter。

## Missing values and units

- Missing、unknown、not returned 與 zero 是不同狀態。
- 不得把缺失 percentage 轉成 0 或 100。
- 不得 silently clamp 超出 0–100 的 percentage；應拒絕本次 provider result。
- Cursor upstream milliseconds 必須在 adapter boundary 轉成 Unix seconds。
- JSON Schema `integer` 依 Draft 2020-12：整數值 JSON number（例如 `2000000000.0`）合法。Runtime model 接受後正規化成 JSON integer；`2000000000.5` 這類非整數必須拒絕。
- Currency value 的單位必須由 provider 文件明確定義；不得混用 cents 與 major currency units。
- 對外 machine-readable contract 是 `schemas/usage-v0.1.schema.json`。`UsageSnapshot.model_json_schema()` 不是 GET /usage 或 OpenAPI contract。

## Freshness and fallback

- `generated_at` 是 response 生成時間，不代表每個 provider 的成功收集時間。
- Freshness 由 `collected_at + stale_after_seconds` 判斷。
- Refresh 失敗且存在 last-good data 時，保留原 meter 與原 `collected_at`，provider 改為 `stale` 並附 sanitized `error`。
- Refresh 失敗不能用空 meter 覆蓋 last-good data。
- Collector restart 後可從 atomic cache 恢復 last-good snapshot，但必須重新計算 stale 狀態。
- Desktop service 停用某 provider 時，內部 last-good 可保留，但不得出現在 GET /usage，也不得把 `collected_at` 改成現在。

## Compatibility

- 新增 optional field 或新 provider 可以留在同一 minor contract，只要舊 consumer 能忽略未知 provider。
- 移除／改名 field、改變單位或 status semantics 是 breaking change，必須更新 `schema_version`。
- ESP32 對未知 provider 或 meter 應安全略過，不得 crash。
