# Cursor Adapter Contract

## Purpose and non-goals

取得 Cursor 個人方案的 Cursor Models、Other Models 與 billing-cycle usage。這是 unofficial adapter；不得把 Team／Enterprise Admin API 當成個人方案 contract，也不得使用 browser scraping。

## Data source and stability

- Connect RPC：`DashboardService/GetCurrentPeriodUsage`。
- Source ID：`cursor_dashboard_connect_rpc`。
- Parser entry：`agent_meter.providers.cursor.collect_period_usage`。這個函式只吃已解碼的 JSON，不讀 login state、不組 request、不發 network。
- `planUsage.autoPercentUsed` → `cursor_models` quota meter。
- `planUsage.apiPercentUsed` → `other_models` quota meter。
- 頂層 `billingCycleEnd` → 有產出的 meter 共用的 `reset_at`。
- Upstream billing cycle timestamp 是 milliseconds；normalized contract 一律轉為 Unix seconds（整數除以 1000）。小於 `10_000_000_000` 的值看起來像秒，省略 `reset_at`，不除成 1970。

因為不是 official public Individual Usage API，任何 Cursor upgrade 都可能改變 endpoint、headers 或 response shape。

## Authentication boundary

- Adapter 只讀使用 Cursor 已登入的 local state。
- Access token、machine identifier 與 checksum 只能存在 request construction boundary。
- 不得 log、cache、fixture 或回傳這些值。
- Token refresh 若會修改 Cursor-owned state，必須由獨立 task 明確授權；v0.1 預設遇到過期 token 回傳 `auth_expired`。
- Cursor IDE 關閉時，只要既有 local session 有效，adapter 應仍可運作。

## Field behavior

- `remaining_percentage = 100 - percentUsed`。
- 兩個 quota pool 分開判斷。Percentage 缺失、非數字、boolean、NaN/Inf 或超出 0–100 時，省略該 pool，不 clamp、不補 0。
- 至少一個 quota pool 合法即為 success。兩個都不合法時回傳 `malformed_response`，即使 on-demand spend 有值。
- `autoSpend`、`autoLimit`、`apiSpend`、`apiLimit` 是 optional included-pool cents。v0.1 不把它們放進 percent quota meter，避免把金額混進 percentage unit。欄位存在或缺失都不改變 success。
- On-demand 來自 `spendLimitUsage.individualUsed` / `individualLimit` / `individualRemaining`，單位是 cents。Normalized `on_demand` spend meter 使用 USD major units：cents / 100。
- `individualUsed` 缺失時省略整個 spend meter。`0` cents 是真實的 0，仍產出 meter。`individualLimit` 小於等於 0 時省略 `limit`。
- 非整百且大到無法轉成 float 的 cents 視為無法安全轉換：`individualUsed` 省略整顆 spend meter，`individualLimit`／`individualRemaining` 只省略該欄位。不得讓這個轉換例外中斷合法 quota meters。

## Failure mapping

- Cursor local state 不存在 → `not_installed`。
- 沒有 login state → `not_authenticated`。
- 401／明確 token expiry → `auth_expired`。
- Deadline exceeded → `timeout`。
- DNS／connection failure → `network`。
- Non-success upstream response → `upstream`。
- JSON／required field／unit 不符 → `malformed_response`。

Last-good fallback 由 orchestrator 負責；Cursor schema/auth 失敗不得影響 Codex 或 Claude。

## Offline fixtures

Sanitized fixtures 在 `tests/fixtures/providers/cursor/`。`happy.json` 植入 fake secret，用來證明 output 不會回顯 email、token、machine id、`autoSpend` 或 raw envelope。Parser tests 不讀 Cursor local state，也不呼叫 Connect RPC。

## Manual smoke test

1. 說明將唯讀使用 Cursor local login state 並發出 network request。
2. 執行 adapter，只顯示 normalized values。
3. 與 Cursor Settings 的 Usage/Spending 值在約定 rounding tolerance 內比對。
4. 關閉 Cursor IDE 後重試一次。
5. 確認 log 與 error 不含 token、machine ID、checksum 或 raw response。

## Upgrade signals

- Connect RPC endpoint/method not found。
- Required client headers 改變。
- `autoPercentUsed`／`apiPercentUsed` 改名或移動。
- Billing cycle timestamp 單位改變。
- Cursor client storage format 或 token lifecycle 改變。
