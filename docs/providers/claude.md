# Claude Code Adapter Contract

## Purpose and non-goals

從 Claude Code structured status-line input 取得 5-hour 與 7-day quota。不得 scrape Claude.ai，不得將 `/usage` 文字解析當成 production data source。

## Data source and stability

- Ingestion boundary：Claude Code status-line command 的 stdin JSON。
- Source ID：`claude_statusline`。
- `rate_limits.five_hour.used_percentage` → `five_hour` meter。
- `rate_limits.seven_day.used_percentage` → `weekly` meter。
- `resets_at` 預期為 UTC Unix seconds。

Status-line data 是 event-driven input，不是 Collector 主動 polling endpoint。Claude Code session 需實際收到 API response 後，rate-limit fields 才可能出現。

## Authentication boundary

Authentication 由 Claude Code 管理。Ingestion command 不讀取、不保存 Anthropic credential，只處理 Claude Code 傳入的 structured data。

## Input and stdout behavior

- stdin 必須 bounded read，並驗證為單一 JSON payload。
- stdout 若輸出 normalized JSON，就不能含 prompt、progress 或 warning。
- Human diagnostics 全部送 stderr。
- Empty stdin、malformed JSON 或缺少 `rate_limits` 不得覆蓋 last-good data。

## Field behavior

- `remaining_percentage = 100 - used_percentage`。
- Percentage 缺失、非數字或超出 0–100 時回傳 `malformed_response`。
- 不得 silently clamp。
- `resets_at` 缺失時是否接受 meter，需由 implementation task 依 UI requirement 明確決定。

## Failure mapping

- Status-line ingestion 未設定 → `not_configured`。
- Claude Code session 不可用 → `not_authenticated`。
- Empty／malformed input 或 required field 缺失 → `malformed_response`。
- 寫入 Collector local handoff 失敗 → `internal`；不得破壞已存在的 last-good snapshot。

## Manual smoke test

1. 在已登入的 Claude Code session 觸發至少一次 API response。
2. 執行 status-line ingestion，確認只產生 normalized/sanitized output。
3. 與 Claude Code `/usage` 顯示值在約定 rounding tolerance 內比對。
4. 測試沒有 `rate_limits` 的 payload，確認 last-good data 保留且 diagnostics 在 stderr。

## Upgrade signals

- `rate_limits` 不再出現在 status-line input。
- `five_hour`／`seven_day` field 改名。
- `resets_at` 單位改變。
- Claude Code 改變 status-line command 的 stdin/stdout lifecycle。
