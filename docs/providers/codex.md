# Codex Adapter Contract

## Purpose and non-goals

取得 Codex 5-hour 與 weekly quota，轉成共用 quota meters。不得 parse `/status` 顯示文字，不得直接呼叫 private OpenAI backend，也不自行處理 ChatGPT credential。

## Data source and stability

- 啟動介面：`codex app-server --stdio`。
- Handshake：`initialize`（id=1）→ `initialized` notification → `account/rateLimits/read`（id=3）。
- Source ID：`codex_app_server`。
- 此介面是 machine-readable，但仍須防止 CLI upgrade 後欄位改變。

## Authentication boundary

Authentication 由 Codex app-server 管理。Adapter 不讀取、不輸出、不保存 token、cookie 或 API key。Child process 只繼承 allowlisted environment（`PATH`、`HOME`、locale、temp、`CODEX_HOME` 等），不繼承完整 parent environment。

## Command and stdout behavior

- Library entry：`agent_meter.providers.codex.collect`。
- CLI：`python -m agent_meter.providers.codex --live`。沒有 `--live` 時不得啟動 app-server，exit `2`。
- `--live` 會沿用本機已登入的 Codex session，並可能對 OpenAI backend 發出 rate-limit 查詢。
- 每次 collect 啟動短生命週期 process；timeout 或成功後都必須回收，先 SIGTERM 再 SIGKILL，含 process group。
- Child stdout 是 JSON-RPC JSONL channel。非 JSON 行、過長行、EOF、缺 `result` 都是 `malformed_response`。
- Child stderr 直接丟棄，不得轉發。Adapter 自己的 diagnostics 寫入 caller stderr。
- JSON-RPC response 依 request `id` 配對；notification（無 `id`）忽略，可在 stderr 留下不含 params 的觀察訊息。

## Upstream mapping

- `rateLimits.primary` → `five_hour` quota meter。
- `rateLimits.secondary` → `weekly` quota meter。
- `usedPercent` → `remaining_percentage = 100 - usedPercent`。
- `windowDurationMins` 用於驗證 window identity：primary 預期 `300`，secondary 預期 `10080`。缺失時仍依 primary／secondary key 映射；值存在但不符則略過該 window。
- `resetsAt` 預期為 UTC Unix seconds。整數值 float 正規化成 int；看起來像毫秒（`>= 10_000_000_000`）或無效值則省略 `reset_at`，不自動換算。
- `rateLimitsByLimitId` 在 v0.1 忽略。Mapping 鎖定 primary／secondary，不得在缺少 tests 的情況下改走 limit-id lookup。

不得 silently clamp `usedPercent`。Percentage 缺失、非數字、boolean、NaN/Inf 或超出 0–100 時，該 window 不產出 meter。兩個 window 可獨立缺席；至少一個合法 meter 即為 success。兩個 window 都無法產出 meter 時回傳 `malformed_response`。

## Failure mapping

- Command 不存在 → `not_installed`。
- App-server 無登入狀態（JSON-RPC error 含 unauthenticated／unauthorized／not logged in 等信號）→ `not_authenticated`。不得複製 upstream error.message。
- Deadline exceeded → `timeout`。
- Spawn／broken pipe／非 auth 的 JSON-RPC error／非零退出 → `upstream`。
- EOF、mixed stdout、malformed JSON、缺 `result` 或缺合法 meter → `malformed_response`。

Last-good fallback 由 orchestrator 負責。Adapter 不寫 cache。

## Offline fixtures

Sanitized fixtures 在 `tests/fixtures/providers/codex/`。`happy.json` 與 fake app-server 含植入的 fake secret，用來證明 output／error 不會回顯 unknown fields、RPC error message 或 child env。Transport tests 使用 `fake_app_server.py`，不呼叫 live `codex`。

## Manual smoke test

1. 在已登入的 Codex 環境執行 `uv run --locked python -m agent_meter.providers.codex --live`。
2. 只輸出 normalized provider snapshot。
3. 與 Codex `/status` 或 status line 顯示的 remaining percentage 與 reset time 比對。
4. 不保存 raw JSON-RPC response，不在 terminal 顯示 credential-related metadata。

## Upgrade signals

- JSON-RPC method not found。
- `primary`／`secondary` 消失或 window duration 改變。
- `resetsAt` 單位改變。
- App-server stdout 出現非 protocol 文字。
- `initialize` 開始要求 `jsonrpc`／`protocolVersion` 等既有 handshake 沒送的欄位。
