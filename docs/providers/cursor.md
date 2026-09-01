# Cursor Adapter Contract

## Purpose and non-goals

取得 Cursor 個人方案的 Cursor Models、Other Models 與 billing-cycle usage。這是 unofficial adapter；不得把 Team／Enterprise Admin API 當成個人方案 contract，也不得使用 browser scraping。

## Data source and stability

- Connect RPC：`DashboardService/GetCurrentPeriodUsage`。
- Source ID：`cursor_dashboard_connect_rpc`。
- `autoPercentUsed` → `cursor_models` quota meter。
- `apiPercentUsed` → `other_models` quota meter。
- `billingCycleEnd` → 兩個 meter 的 `reset_at`。
- Upstream billing cycle timestamp 是 milliseconds；normalized contract 一律轉為 Unix seconds。

因為不是 official public Individual Usage API，任何 Cursor upgrade 都可能改變 endpoint、headers 或 response shape。

## Authentication boundary

- Adapter 只讀使用 Cursor 已登入的 local state。
- Access token、machine identifier 與 checksum 只能存在 request construction boundary。
- 不得 log、cache、fixture 或回傳這些值。
- Token refresh 若會修改 Cursor-owned state，必須由獨立 task 明確授權；v0.1 預設遇到過期 token 回傳 `auth_expired`。
- Cursor IDE 關閉時，只要既有 local session 有效，adapter 應仍可運作。

## Field behavior

- `remaining_percentage = 100 - percentUsed`。
- Percentage 缺失、非數字或超出 0–100 時，對應 required meter 視為 malformed。
- `autoSpend`、`autoLimit`、`apiSpend`、`apiLimit` 與 on-demand spend 都是 optional。
- Upstream 沒有回傳 spend value 時，省略 spend meter，不建立假資料或全 `null` object。
- 若只有其中一個 quota pool 有效，是否接受部分 provider result 必須在 implementation task 明確決定；不得自行猜測。

## Failure mapping

- Cursor local state 不存在 → `not_installed`。
- 沒有 login state → `not_authenticated`。
- 401／明確 token expiry → `auth_expired`。
- Deadline exceeded → `timeout`。
- DNS／connection failure → `network`。
- Non-success upstream response → `upstream`。
- JSON／required field／unit 不符 → `malformed_response`。

Last-good fallback 由 orchestrator 負責；Cursor schema/auth 失敗不得影響 Codex 或 Claude。

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
