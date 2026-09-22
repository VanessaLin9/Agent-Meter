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

- Ingest command：`python -m agent_meter.providers.claude`。這是 Collector parser，stdout 是 typed JSON，不是 Claude Code 畫面上的 status line。
- stdin 必須 bounded read（預設 1 MiB），並驗證為單一 JSON payload；後面若還有非空白內容視為 malformed。
- 超大 JSON 整數（`ValueError`）、過深巢狀（`RecursionError`）與非標準 `NaN`／`Infinity` 一律當 malformed JSON，不得讓 CLI 在寫出 typed result 前崩潰，也不得因 unknown field 裡的非標準常數而 success。
- stdout 永遠是一行 machine-readable JSON（`result=success|failure`），不能含 prompt、progress 或 warning。
- Human diagnostics 全部送 stderr；成功時 stderr 保持空白。
- Empty stdin、malformed JSON、`rate_limits` 缺失／null，或沒有任何合法 meter 時回傳 typed failure，由 orchestrator 決定是否保留 last-good data。
- 不得把 unknown status-line fields（account、session、transcript path）複製到 result、log 或 error message。

## Field behavior

- `remaining_percentage = 100 - used_percentage`。
- Percentage 缺失、非數字、boolean、NaN/Inf 或超出 0–100 時，該 window 不產出 meter，且不得 silently clamp。
- 兩個 window 可獨立缺席；至少一個合法 meter 即為 success。兩個 window 都無法產出 meter 時回傳 `malformed_response`。
- `resets_at` 缺失、null 或不是非負整數 Unix seconds 時，省略 `reset_at`，不因此拒絕合法 percentage。

## Failure mapping

本 ingest command 只從 stdin 解析 structured data，因此目前只產生 `malformed_response`（empty、oversized、malformed JSON、缺少 `rate_limits`、或沒有合法 meter）。`not_configured`、`not_authenticated` 與 cache write `internal` 留給 orchestrator／後續 handoff，不在本 module 發明假資料。

Last-good fallback 由 orchestrator 依 typed failure 決定；adapter 不寫 cache。

## Offline fixtures

Sanitized fixtures 在 `tests/fixtures/providers/claude/`。`happy.json` 含植入的 fake secret，用來證明 output／error 不會回顯 unknown fields。

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
