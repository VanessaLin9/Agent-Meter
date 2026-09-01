# Codex Adapter Contract

## Purpose and non-goals

取得 Codex 5-hour 與 weekly quota，轉成共用 quota meters。不得 parse `/status` 顯示文字，不得直接呼叫 private OpenAI backend，也不自行處理 ChatGPT credential。

## Data source and stability

- 啟動介面：`codex app-server --stdio`。
- JSON-RPC method：`account/rateLimits/read`。
- Source ID：`codex_app_server`。
- 此介面是 machine-readable，但仍須防止 CLI upgrade 後欄位改變。

## Authentication boundary

Authentication 由 Codex app-server 管理。Adapter 不讀取、不輸出、不保存 token、cookie 或 API key。

## Upstream mapping

- `rateLimits.primary` → `five_hour` quota meter。
- `rateLimits.secondary` → `weekly` quota meter。
- `usedPercent` → `remaining_percentage = 100 - usedPercent`。
- `windowDurationMins` 用於驗證 window identity，不直接當 reset time。
- `resetsAt` 預期為 UTC Unix seconds。
- `rateLimitsByLimitId.codex` 可作 future-proof lookup，但 mapping 必須由 tests 固定。

不得 silently clamp `usedPercent`；缺欄位、非數字或超出 0–100 時回傳 `malformed_response`。

## Process behavior

- 完成 JSON-RPC initialize handshake 後才呼叫 rate-limit method。
- Response 依 request `id` 配對；忽略但可觀察非目標 notification。
- MVP 可以每次 refresh 啟動短生命週期 process；是否常駐由後續量測決定。
- Timeout 或 cancellation 後必須終止 child process，不留下 orphan。
- stdout 視為 JSON-RPC channel；diagnostics 只能走 stderr。

## Failure mapping

- Command 不存在 → `not_installed`。
- App-server 無登入狀態 → `not_authenticated`。
- Deadline exceeded → `timeout`。
- Backend transport failure → `network` 或 `upstream`。
- EOF、empty/mixed output、missing fields → `malformed_response`。

Last-good fallback 由 orchestrator 負責。

## Manual smoke test

1. 在已登入的 Codex 環境執行 adapter。
2. 只輸出 normalized provider snapshot。
3. 與 Codex `/status` 或 status line 顯示的 remaining percentage 與 reset time 比對。
4. 不保存 raw JSON-RPC response，不在 terminal 顯示 credential-related metadata。

## Upgrade signals

- JSON-RPC method not found。
- `primary`／`secondary` 消失或 window duration 改變。
- `resetsAt` 單位改變。
- App-server stdout 出現非 protocol 文字。
